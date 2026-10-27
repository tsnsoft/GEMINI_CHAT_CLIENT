import sys
import os
import html  # html.escape() — экранируем пользовательский текст перед вставкой в QTextEdit как HTML
import webbrowser  # нужен, чтобы открыть страницу в браузере по умолчанию
import requests  # библиотека для HTTP-запросов к Gemini API
from PyQt5.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
                             QHBoxLayout, QTextEdit, QLineEdit, QPushButton,
                             QLabel, QMessageBox, QComboBox)
from PyQt5.QtCore import QThread, pyqtSignal  # QThread — фоновый поток, pyqtSignal — сигналы между потоками
from PyQt5.QtGui import QTextCursor  # курсор нужен, чтобы точечно редактировать текст в QTextEdit

# Страница получения собственного бесплатного API-ключа.
# Каждый студент должен создать СВОЙ ключ здесь, а не пользоваться чужим —
# общий ключ = общая квота на всех и риск, что кто-то её исчерпает или сольёт ключ.
API_KEY_PAGE_URL = "https://aistudio.google.com/app/api-keys"

# Список доступных моделей вынесен в константу (а не "зашит" в код).
# При отключении какой-то модели Google достаточно поменять значение здесь,
# не переписывая логику всего приложения.
AVAILABLE_MODELS = [
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
]

# Шаблон URL эндпоинта. {model} подставляется динамически в зависимости
# от того, что выбрал пользователь в выпадающем списке.
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiWorker(QThread):
    """
    Фоновый поток для сетевого запроса к Gemini API.
    Запрос выполняется НЕ в главном потоке, иначе интерфейс "зависал бы"
    (не перерисовывался бы и не реагировал на клики) на всё время ожидания ответа.
    """
    # Сигналы — способ безопасно передать данные из фонового потока обратно
    # в главный поток интерфейса (напрямую трогать виджеты из QThread нельзя).
    finished_signal = pyqtSignal(str)  # сработает при успешном ответе, несёт текст ответа
    error_signal = pyqtSignal(str)     # сработает при ошибке, несёт текст ошибки

    def __init__(self, api_key, model, prompt):
        super().__init__()
        self.api_key = api_key  # ключ пользователя, введённый в интерфейсе
        self.model = model      # выбранная модель (например, gemini-3.6-flash)
        self.prompt = prompt    # текст запроса от пользователя

    def run(self):
        """Метод автоматически вызывается при старте потока (self.worker.start())."""
        url = API_URL_TEMPLATE.format(model=self.model)  # подставляем модель в URL
        headers = {"Content-Type": "application/json"}   # сообщаем серверу, что тело запроса — JSON
        payload = {
            # Формат тела запроса, который ожидает Gemini API:
            # список "контентов", каждый содержит список "частей" с текстом.
            "contents": [
                {"parts": [{"text": self.prompt}]}
            ]
        }

        try:
            # params={"key": ...} — ключ передаётся как query-параметр в URL.
            # Это не единственный вариант: Gemini API также принимает ключ
            # в заголовке x-goog-api-key — здесь выбран query-параметр просто
            # как более короткий и наглядный для учебного примера способ.
            # timeout=45 — ограничение именно на сетевые операции запроса
            # (соединение и ожидание данных), а не жёсткий предел на общую
            # длительность всей операции — при некоторых сценариях (например,
            # медленной потоковой передаче данных) фактическое время может
            # отличаться.
            response = requests.post(
                url,
                params={"key": self.api_key},
                json=payload,
                headers=headers,
                timeout=45,
            )
        except requests.exceptions.Timeout:
            # Отдельно ловим таймаут — самая частая проблема при нестабильной сети/VPN.
            self.error_signal.emit("Таймаут соединения (45 сек). Проверьте стабильность сети/VPN!")
            return
        except requests.exceptions.ConnectionError:
            # Отдельно ловим обрыв соединения — сервер вообще не отвечает.
            self.error_signal.emit("Не удалось связаться с серверами Google. Проверьте сеть/VPN!")
            return
        except requests.exceptions.RequestException as e:
            # Любая другая сетевая ошибка requests, которую мы не предусмотрели отдельно.
            self.error_signal.emit(f"Ошибка сети: {e}")
            return

        # Сначала пробуем распарсить тело ответа как JSON.
        # Если сервер вернул не-JSON (например, HTML-страницу ошибки от
        # прокси/VPN или вообще пустое тело), response.json() выбросит ValueError.
        try:
            data = response.json()
        except ValueError:
            # Печатаем "сырой" ответ в консоль — это важно для отладки,
            # иначе студенты увидят только общее сообщение об ошибке и не поймут причину.
            print("--- RAW RESPONSE (не JSON) ---")
            print("Status:", response.status_code)
            print(response.text[:1000])  # ограничиваем вывод, чтобы не засорять консоль
            print("-------------------------------")
            self.error_signal.emit(
                f"Сервер вернул не-JSON ответ (код {response.status_code}). "
                f"Подробности — в консоли."
            )
            return

        # ИСПРАВЛЕНО: весь разбор структуры ответа обёрнут в try/except.
        # Сервер по спецификации почти всегда возвращает ожидаемую структуру,
        # но если где-то в JSON вместо словаря окажется None — например,
        # {"candidates": [{"content": null}]} — то .get("content", {}) вернёт
        # именно None (значение по умолчанию в .get() подставляется только
        # при ОТСУТСТВИИ ключа, а не когда он есть, но равен null), и
        # следующий .get("parts", []) на None упадёт с AttributeError.
        # Без этого try/except такое исключение прервало бы run() ДО вызова
        # error_signal — пользователь не увидел бы вообще никакого сообщения
        # об ошибке: сигнал QThread.finished всё равно сработает и
        # on_worker_finished() молча разблокирует интерфейс.
        try:
            # HTTP 200 означает, что запрос дошёл и обработан успешно.
            if response.status_code == 200:
                # Структура успешного ответа: data -> candidates -> [0] -> content -> parts -> [...] -> text
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        # Часть элементов parts может не иметь текста вовсе
                        # (тогда join() тихо даёт пустую строку — и мы бы
                        # отправили finished_signal с пустым ответом, что
                        # выглядело бы как "успех" без содержимого). Кроме
                        # того, у моделей линейки 3.x среди parts могут
                        # встречаться части с "thought": true — это сводки
                        # рассуждений модели, которые API предоставляет
                        # отдельно от основного ответа. Их можно было бы
                        # показывать пользователю особым образом (например,
                        # в сворачиваемом блоке "ход мыслей"), но в этом
                        # простом интерфейсе решено показывать только
                        # финальный ответ, поэтому исключаем их из склейки.
                        text = "".join(
                            p["text"]
                            for p in parts
                            if isinstance(p, dict)
                            and isinstance(p.get("text"), str)
                            and not p.get("thought", False)
                        )

                        if not text.strip():
                            self.error_signal.emit(
                                "Модель вернула ответ без пользовательского текста."
                            )
                            return

                        self.finished_signal.emit(text)  # отправляем текст ответа в главный поток
                        return
                # Если candidates пустой — модель ничего не сгенерировала,
                # часто это значит, что сработал фильтр безопасности (safety filter).
                self.error_signal.emit("Пустой ответ от модели (возможно, сработал safety-фильтр).")
            else:
                # Любой код, отличный от 200 (400, 403, 404, 429, 500 и т.д.) — ошибка.
                # Google обычно кладёт подробности в поле "error" тела ответа.
                err_info = data.get("error", {})
                code = err_info.get("code", response.status_code)
                msg = err_info.get("message", response.text)
                self.error_signal.emit(f"Ошибка {code}: {msg}")
        except (TypeError, AttributeError, KeyError, IndexError) as e:
            # Сюда попадаем, если структура JSON оказалась не такой, как мы
            # ожидали (например, content: null вместо словаря). Показываем
            # понятное сообщение вместо того, чтобы молча уронить поток.
            self.error_signal.emit(f"Неожиданная структура ответа сервера: {e}")


class GeminiChatApp(QMainWindow):
    """Главное окно приложения — вся разметка интерфейса и логика взаимодействия с пользователем."""

    # Текст-заглушка, который показывается, пока ждём ответ от модели.
    PLACEHOLDER_TEXT = "Gemini генерирует ответ..."

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gemini AI Client (PyQt5) — учебная версия")
        self.resize(750, 620)  # стартовый размер окна в пикселях
        self.worker = None  # сюда будем класть текущий фоновый поток запроса

        # Запоминаем позицию курсора, с которой начинается временный текст
        # "генерирует ответ...", чтобы потом стереть именно его, а не
        # пытаться искать этот текст внутри HTML (это ненадёжно).
        self._placeholder_start = None

        self.init_ui()  # строим интерфейс

    def init_ui(self):
        """Собирает все виджеты и раскладывает их по layout-ам."""

        # --- Строка с полем ввода API-ключа ---
        key_layout = QHBoxLayout()  # горизонтальное расположение элементов в строке
        key_label = QLabel("API Key:")
        self.key_input = QLineEdit()
        self.key_input.setEchoMode(QLineEdit.Password)  # скрываем ключ звёздочками при вводе
        self.key_input.setPlaceholderText("Вставьте ваш AIzaSy... ключ")

        # Если ключ задан через переменную окружения GEMINI_API_KEY,
        # подставляем его автоматически, чтобы не вводить каждый раз вручную.
        saved_key = os.getenv("GEMINI_API_KEY", "")
        if saved_key:
            self.key_input.setText(saved_key)

        # Кнопка открывает страницу AI Studio, где студент
        # создаёт СВОЙ бесплатный ключ (см. комментарий у API_KEY_PAGE_URL).
        self.btn_get_key = QPushButton("Получить ключ")
        self.btn_get_key.setToolTip("Откроет aistudio.google.com — создайте там свой бесплатный API-ключ")
        self.btn_get_key.clicked.connect(self.open_api_key_page)  # подключаем обработчик клика

        key_layout.addWidget(key_label)
        key_layout.addWidget(self.key_input)
        key_layout.addWidget(self.btn_get_key)

        # --- Строка с выбором модели ---
        # Наглядно показывает студентам, что имя модели — не "вшитая" константа,
        # а параметр, который можно и нужно менять.
        model_layout = QHBoxLayout()
        model_label = QLabel("Модель:")
        self.model_select = QComboBox()
        self.model_select.addItems(AVAILABLE_MODELS)  # заполняем список из константы выше
        model_layout.addWidget(model_label)
        model_layout.addWidget(self.model_select)
        model_layout.addStretch()  # "пружина" — прижимает виджеты влево, оставляя пустое место справа

        # --- Окно истории переписки ---
        self.chat_history = QTextEdit()
        self.chat_history.setReadOnly(True)  # пользователь не может редактировать историю вручную
        self.chat_history.setStyleSheet("background: #fdfdfd; font-size: 13px; line-height: 1.4;")

        # --- Строка ввода сообщения и кнопка отправки ---
        input_layout = QHBoxLayout()
        self.msg_input = QLineEdit()
        self.msg_input.setPlaceholderText("Введите ваш запрос...")
        self.msg_input.returnPressed.connect(self.send_message)  # отправка по Enter

        self.btn_send = QPushButton("Отправить")
        self.btn_send.setStyleSheet("background: #0d47a1; color: white; font-weight: bold; padding: 6px 16px;")
        self.btn_send.clicked.connect(self.send_message)  # отправка по клику мышью

        input_layout.addWidget(self.msg_input)
        input_layout.addWidget(self.btn_send)

        # --- Собираем все блоки в единую вертикальную компоновку ---
        layout = QVBoxLayout()
        layout.addLayout(key_layout)
        layout.addLayout(model_layout)
        layout.addWidget(self.chat_history)
        layout.addLayout(input_layout)

        # QMainWindow требует "центральный виджет" — контейнер, в который
        # вкладывается весь остальной интерфейс.
        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

    def open_api_key_page(self):
        """Открывает страницу создания API-ключа в браузере по умолчанию."""
        webbrowser.open(API_KEY_PAGE_URL)

    def send_message(self):
        """Вызывается при нажатии Enter в поле ввода или клике на кнопку 'Отправить'."""
        # Защита самого метода от повторного запуска, пока предыдущий запрос
        # не завершён. Отключённые виджеты (btn_send, msg_input) уже не дают
        # пользователю отправить второе сообщение через интерфейс, но эта
        # проверка защищает сам send_message() и в случае, если его вызовет
        # какой-то другой код (например, горячая клавиша или будущая функция),
        # а не только клик по кнопке.
        if self.worker is not None:
            return

        api_key = self.key_input.text().strip()  # strip() убирает случайные пробелы по краям
        prompt = self.msg_input.text().strip()
        model = self.model_select.currentText()  # текущая выбранная модель из выпадающего списка

        if not api_key:
            # Без ключа отправлять запрос бессмысленно — предупреждаем пользователя.
            QMessageBox.warning(self, "Внимание", "Введите API Key от Google AI Studio (начинается на AIzaSy...)")
            return
        if not prompt:
            # Пустое сообщение просто игнорируем, ничего не отправляем.
            return

        # Показываем в истории сообщение пользователя.
        # ИСПРАВЛЕНО: раньше prompt вставлялся прямо в HTML без экранирования —
        # если пользователь написал бы что-то вроде "<b>тест</b>" или "<script>",
        # это интерпретировалось бы как разметка, а не как обычный текст.
        # html.escape() превращает спецсимволы (<, >, &, "...) в их HTML-сущности.
        self.chat_history.append(f"<b>Вы:</b> {html.escape(prompt)}")

        # Запоминаем позицию курсора ПЕРЕД вставкой временного текста —
        # это нужно, чтобы потом точно найти и стереть именно этот кусок,
        # а не гадать по содержимому HTML (см. _remove_placeholder).
        cursor = self.chat_history.textCursor()
        cursor.movePosition(QTextCursor.End)  # перемещаем курсор в конец текста
        self._placeholder_start = cursor.position()  # запоминаем числовую позицию
        self.chat_history.append(f"<i>{self.PLACEHOLDER_TEXT}</i>")  # показываем "генерирует ответ..."

        self.msg_input.clear()  # очищаем поле ввода для следующего сообщения

        # Блокируем интерфейс на время ожидания ответа, чтобы пользователь
        # не отправил второй запрос поверх ещё не завершённого первого.
        self.btn_send.setEnabled(False)
        self.msg_input.setEnabled(False)

        # Создаём и запускаем фоновый поток с запросом к API.
        self.worker = GeminiWorker(api_key, model, prompt)
        self.worker.finished_signal.connect(self.on_success)  # что делать при успехе
        self.worker.error_signal.connect(self.on_error)        # что делать при ошибке
        # QThread.finished — встроенный сигнал Qt, срабатывает при завершении
        # run() независимо от того, был ли это успех или ошибка. Разблокировку
        # интерфейса и очистку ссылки на поток привязываем именно к нему —
        # это надёжнее, чем дублировать unlock_ui() в on_success() и on_error()
        # по отдельности, и гарантирует, что self.worker не останется висеть
        # на уже отработавшем объекте.
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()  # запускает run() потока в отдельном потоке ОС

    def _remove_placeholder(self):
        """Стирает временное сообщение 'генерирует ответ...' по сохранённой позиции."""
        if self._placeholder_start is None:
            return  # заглушки не было — нечего стирать

        cursor = self.chat_history.textCursor()
        cursor.setPosition(self._placeholder_start)  # ставим курсор в начало заглушки
        # Выделяем весь текст от начала заглушки до конца документа,
        # удерживая якорь (KeepAnchor) — так формируется выделение, а не просто перемещение.
        # В выделение попадает и разделитель абзаца, который append() сам
        # вставил перед текстом заглушки — так что после removeSelectedText()
        # он тоже будет удалён, отдельная команда для этого не нужна.
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.removeSelectedText()  # удаляем выделение целиком: разделитель абзаца + текст заглушки
        # ИСПРАВЛЕНО: здесь раньше стоял cursor.deletePreviousChar() — он был лишним
        # и удалял ПОСЛЕДНИЙ СИМВОЛ сообщения пользователя (removeSelectedText()
        # уже стёр всё нужное, включая разделитель абзаца перед заглушкой).
        self._placeholder_start = None  # сбрасываем — заглушки больше нет

    def on_success(self, text):
        """Обработчик сигнала finished_signal — вызывается при успешном ответе модели."""
        self._remove_placeholder()  # убираем "генерирует ответ..."
        # ИСПРАВЛЕНО: текст от модели экранируем (защита от случайных HTML-
        # спецсимволов в ответе), а затем переносим строки вручную через <br>.
        # html.escape() трогает только <, >, &, кавычки — обычные символы
        # новой строки \n он не превращает в видимый перенос, поэтому
        # многострочный ответ без .replace() схлопнулся бы в одну строку.
        safe_text = html.escape(text).replace("\n", "<br>")
        self.chat_history.append(f"<b>Gemini:</b> {safe_text}<br>")  # выводим ответ модели
        # unlock_ui() отсюда убран — теперь разблокировкой занимается
        # on_worker_finished(), привязанный к сигналу QThread.finished.

    def on_error(self, err_msg):
        """Обработчик сигнала error_signal — вызывается при любой ошибке запроса."""
        self._remove_placeholder()
        # По той же причине, что и в on_success(), экранируем текст и
        # заменяем переносы строк — сообщения об ошибках от сервера иногда
        # тоже приходят многострочными.
        safe_err = html.escape(err_msg).replace("\n", "<br>")
        self.chat_history.append(f"<b style='color: red;'>Ошибка:</b> {safe_err}<br>")
        # unlock_ui() отсюда тоже убран — см. комментарий в on_success().

    def on_worker_finished(self):
        """
        Вызывается автоматически по сигналу QThread.finished — когда run()
        потока завершился, независимо от того, был это успех или ошибка.
        Здесь, а не в on_success()/on_error(), выполняется очистка и
        разблокировка интерфейса — так это гарантированно происходит ровно
        один раз для каждого запроса, и объект потока не остаётся "висеть".
        """
        worker = self.worker
        self.worker = None  # обнуляем ссылку — этот запрос полностью завершён

        if worker is not None:
            # deleteLater() просит Qt безопасно удалить объект потока,
            # когда это будет уместно (после возврата в цикл событий) —
            # так его ресурсы освобождаются явно, а не когда-нибудь потом
            # сборщиком мусора Python.
            worker.deleteLater()

        self.statusBar().clearMessage()
        self.unlock_ui()

    def unlock_ui(self):
        """Возвращает интерфейс в рабочее состояние после завершения запроса (успех или ошибка)."""
        self.btn_send.setEnabled(True)
        self.msg_input.setEnabled(True)
        self.msg_input.setFocus()  # сразу ставим курсор в поле ввода для удобства

    def closeEvent(self, event):
        """Вызывается автоматически при попытке закрыть окно."""
        # Пока поток выполняется — не разрешаем окну закрыться (event.ignore()),
        # а сообщаем об этом в строке состояния (ненавязчиво, без модального
        # окна, которое требовало бы отдельного клика).
        # Не дёргаем terminate() — принудительное убийство потока небезопасно
        # (поток может оборваться прямо в середине системного вызова), а
        # безусловный event.accept() после короткого wait() тоже не годится:
        # если поток не успел завершиться, Python может удалить объект
        # QThread, пока внутри него ещё выполняется сетевой запрос — Qt в
        # такой ситуации выводит предупреждение
        # "QThread: Destroyed while thread is still running" и может привести
        # к падению приложения.
        if self.worker is not None and self.worker.isRunning():
            self.statusBar().showMessage(
                "Запрос ещё выполняется. Дождитесь завершения и закройте окно повторно."
            )
            event.ignore()  # закрытие окна отменяется
            return
        event.accept()  # поток не выполняется — можно закрывать окно


if __name__ == "__main__":
    app = QApplication(sys.argv)      # создаём приложение Qt
    win = GeminiChatApp()             # создаём главное окно
    win.show()                        # показываем окно на экране
    sys.exit(app.exec_())             # запускаем цикл обработки событий, выходим с его кодом возврата
