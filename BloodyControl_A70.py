"""Bloody Control A70 v1.0.1
A practical HID utility for the user's A70:
- confirmed backlight level read/write, with read-back verification
- software lighting animations composed only from the working brightness command
- unlimited local presets, import/export
- auto-off timer
- device/firmware query and HID interface diagnostics
- background DPI apply/readback tasks, keeping the UI responsive when a driver is slow

No RGB/effect packets or configuration-memory writes are guessed.
"""
from __future__ import annotations

import json
import random
import time
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from pathlib import Path
from datetime import datetime

APP_TITLE = "Bloody Control A70"
VID = 0x09DA
PID = 0x5BAA
REPORT_ID = 0x07
REPORT_LEN = 72
OP_BRIGHTNESS = 0x11
OP_PERSIST = 0x0A
OP_DEVICE_INFO = 0x1F
TARGET_PATH_MARKER = "&mi_02&col05"

BG = "#0B0D14"
SIDEBAR = "#10131D"
PANEL = "#151A27"
PANEL_2 = "#20273A"
PANEL_3 = "#252E45"
TEXT = "#F5F3FF"
MUTED = "#9BA6BE"
PURPLE = "#9B5CFF"
PURPLE_HOVER = "#B184FF"
TEAL = "#50D7C4"
GREEN = "#4ADE80"
RED = "#FB7185"
AMBER = "#FBBF24"
BORDER = "#2A3349"
# A70 / PixArt PMW3327: the supplied Workshop Sensitivity.log has a five-stage
# profile 1000 / 1200 / 1600 / 2000 / 6200 CPI under SENSOR_3327_1.
DPI_VALUES = [1000, 1200, 1600, 2000, 6200]
# Register pairs are calculated from the documented Bloody live-DPI relation:
# CPI ~= 25 * (r + 1) * a. The current stage is selected with opcode 0x0D.
DPI_REGISTER_MAP = {
    1000: (0x0A, 3),
    1200: (0x0C, 3),
    1600: (0x10, 3),
    2000: (0x14, 3),
    6200: (0x1F, 7),
}
DPI_RATE_HZ = {0x01: 1000, 0x02: 500, 0x04: 250, 0x08: 125}
LEVEL_NAMES = {0: "Выключена", 1: "Низкая", 2: "Средняя", 3: "Высокая"}
EFFECTS = {
    "Статика": "Оставляет выбранную яркость",
    "Дыхание": "Плавный ступенчатый цикл 0 → 1 → 2 → 3 → 2 → 1",
    "Пульс": "Мягкое переключение выключено / максимум",
    "Стробоскоп": "Быстрое переключение выключено / максимум",
    "Случайное мерцание": "Случайная яркость 0–3",
    "Двойной импульс": "Два коротких всплеска, затем пауза",
    "SOS": "Международный световой сигнал SOS яркостью",
    "Свой цикл": "Твой собственный цикл уровней 0–3",
}
APP_DIR = Path.home() / "BloodyControlA70"
PROFILES_FILE = APP_DIR / "profiles.json"


def parse_level_sequence(raw: str) -> list[int]:
    tokens = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    if not 2 <= len(tokens) <= 32:
        raise ValueError("Цикл должен содержать от 2 до 32 уровней, например 0,1,2,3,2,1.")
    try:
        levels = [int(token) for token in tokens]
    except ValueError:
        raise ValueError("В цикле нужны только числа 0, 1, 2 или 3.")
    if any(level not in LEVEL_NAMES for level in levels):
        raise ValueError("Каждый уровень цикла должен быть 0, 1, 2 или 3.")
    return levels

def path_text(path) -> str:
    if isinstance(path, bytes):
        return path.decode("utf-8", errors="replace")
    return str(path or "")


def packet(opcode: int, **values) -> bytes:
    data = bytearray(REPORT_LEN)
    data[0] = REPORT_ID
    data[1] = opcode
    for offset, value in values.items():
        data[int(offset)] = int(value) & 0xFF
    return bytes(data)


def brightness_write(level: int) -> bytes:
    if level not in LEVEL_NAMES:
        raise ValueError("Уровень яркости должен быть от 0 до 3")
    data = bytearray(REPORT_LEN)
    data[0] = REPORT_ID
    data[1] = OP_BRIGHTNESS
    data[4] = 0x80
    data[8] = level
    return bytes(data)


def brightness_read_request() -> bytes:
    data = bytearray(REPORT_LEN)
    data[0] = REPORT_ID
    data[1] = OP_BRIGHTNESS
    data[4] = 0x00
    return bytes(data)


def send_feature(device, payload: bytes, label: str = "HID-команда") -> None:
    padded = bytes(payload).ljust(REPORT_LEN, b"\0")[:REPORT_LEN]
    sent = device.send_feature_report(padded)
    if not isinstance(sent, int) or sent <= 0:
        raise RuntimeError(f"Не удалось отправить «{label}»: {sent!r}")


def query_feature(device, payload: bytes, label: str = "чтение HID") -> list[int]:
    send_feature(device, payload, label)
    time.sleep(0.035)
    response = device.get_feature_report(REPORT_ID, REPORT_LEN)
    if not response or len(response) < 2:
        raise RuntimeError(f"Пустой ответ при операции «{label}»")
    return list(response)


def read_brightness(device) -> int:
    response = query_feature(device, brightness_read_request(), "чтение яркости")
    if response[0] != REPORT_ID or response[1] != OP_BRIGHTNESS:
        raise RuntimeError("Ответ на чтение яркости имеет неожиданный формат: " + hex_head(response))
    if len(response) <= 8 or int(response[8]) not in LEVEL_NAMES:
        raise RuntimeError("Мышь вернула неизвестный уровень яркости: " + hex_head(response))
    return int(response[8])


def hex_head(data, count=20) -> str:
    return " ".join(f"{int(b):02X}" for b in list(data)[:count])


def read_device_info(device) -> dict:
    response = query_feature(device, packet(OP_DEVICE_INFO), "чтение модели и прошивки")
    if len(response) < 13 or response[0] != REPORT_ID or response[1] != OP_DEVICE_INFO:
        raise RuntimeError("Команда информации не вернула ожидаемый ответ: " + hex_head(response))
    model_word = response[10] | (response[11] << 8)
    return {
        "model": model_word & 0xFFFC,
        "subtype": model_word & 0x03,
        "firmware": response[12],
        "flags": response[9],
        "raw": response,
    }


def read_current_dpi_slot(device) -> tuple[int | None, list[int] | None]:
    """Read-only DPI-slot probe described for some Bloody HID firmware families.
    Does not change DPI. It toggles vendor status reporting on briefly and always sends
    the corresponding disable packet in a finally block.
    """
    reports_on = packet(0x09, **{"8": 1})
    reports_off = packet(0x09)
    status_request = packet(0x0C, **{"8": 2, "9": 0x80, "10": 1})
    last_report = None
    try:
        send_feature(device, reports_on, "временное включение отчёта DPI")
        time.sleep(0.03)
        # Drain stale input reports if the platform exposes them.
        try:
            for _ in range(8):
                # Never use timeout_ms=0 here: some hidapi builds treat it as a
                # blocking read, so an empty queue can stall the DPI command forever.
                queued = device.read(16, 1)
                if not queued:
                    break
        except Exception:
            pass
        for _ in range(3):
            send_feature(device, status_request, "запрос текущего DPI-слота")
            try:
                report = device.read(16, 180)
            except Exception:
                report = []
            if report:
                last_report = list(report)
                if len(report) > 12 and report[0:3] == [0x07, 0x01, 0x01]:
                    return int(report[12]), last_report
    finally:
        try:
            send_feature(device, reports_off, "выключение отчётов DPI")
        except Exception:
            pass
    return None, last_report


class BloodyControl:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE + " · v1.0")
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        start_w = min(1220, max(980, screen_w - 44))
        start_h = min(820, max(620, screen_h - 70))
        self.root.geometry(f"{start_w}x{start_h}")
        self.root.minsize(min(980, start_w), min(600, start_h))
        self.root.configure(bg=BG)
        self.hid = None
        self.device = None
        self.device_info = None
        self.device_entry = None
        self.hid_entries = []
        self.current_level = None
        self.selected_static_level = 3
        self.effect_name = "Статика"
        self.effect_speed = 5
        self.effect_running = False
        self.effect_after_id = None
        self.effect_step = 0
        self.custom_sequence = [0, 1, 2, 3, 2, 1]
        self.level_test_after_id = None
        self.level_test_running = False
        self.level_test_index = 0
        self.level_test_restore = 2
        self.effect_stop_after_id = None
        self.effect_duration_var = tk.StringVar(value="Без ограничения")
        self.profile_rotation_after_id = None
        self.profile_rotation_active = False
        self.profile_rotation_index = -1
        self.profile_rotation_interval_var = tk.StringVar(value="1 минута")
        self.profile_rotation_status_var = tk.StringVar(value="Автопереключение выключено")
        self.auto_off_after_id = None
        self.auto_off_tick_id = None
        self.auto_off_deadline = None
        self.profiles = self._load_profiles()
        self.last_report = []
        self.log_lines: list[str] = []
        self.scroll_canvases = {}
        self.level_var = tk.IntVar(value=3)
        self.level_text_var = tk.StringVar(value="Высокая · уровень 3")
        self.effect_var = tk.StringVar(value="Статика")
        self.speed_var = tk.IntVar(value=5)
        self.auto_off_var = tk.StringVar(value="Не отключать")
        self.auto_off_status_var = tk.StringVar(value="Таймер не активен")
        self.custom_sequence_var = tk.StringVar(value="0,1,2,3,2,1")
        self.profile_sequence_var = tk.StringVar(value="0,1,2,3,2,1")
        self.status_var = tk.StringVar(value="Подключи мышь кнопкой «Найти A70».")
        self.status_color = MUTED
        self.connected_var = tk.StringVar(value="Не подключено")
        self.hardware_info_var = tk.StringVar(value="Модель и версия прошивки ещё не прочитаны")
        self.brightness_info_var = tk.StringVar(value="Яркость: неизвестна")
        self.dpi_info_var = tk.StringVar(value="Активный DPI-слот ещё не проверен")
        self.dpi_current_value_var = tk.StringVar(value="—")
        self.dpi_current_stage_var = tk.StringVar(value="Подключи мышь, чтобы прочитать DPI")
        self.dpi_selected_var = tk.IntVar(value=1000)
        self.dpi_protocol_status_var = tk.StringVar(value="Протокол DPI ещё не проверен")
        self.current_dpi_slot = None
        # All potentially blocking HID discovery/readback tasks run outside the Tk thread.
        self._worker_queue = queue.Queue()
        self._worker_poll_id = None
        self._closing = False
        self._connect_generation = 0
        self._connect_pending = False
        self._connect_timeout_id = None
        self._device_task_generation = 0
        self._device_task_pending = False
        self._device_task_name = ""
        self._device_task_timeout_id = None
        self.active_page_key = "overview"
        self.active_page_frame = None
        self.nav_items = {}
        self.page_meta = {
            "overview": ("Главная", "Состояние мыши и быстрые действия"),
            "light": ("Подсветка", "Управление яркостью и световыми эффектами"),
            "dpi": ("DPI", "Чувствительность сенсора и активный уровень"),
            "profiles": ("Профили", "Сохраняй сочетания DPI и подсветки"),
            "device": ("Устройство", "Информация и USB/HID диагностика"),
            "automation": ("Автоматизация", "Таймеры и переключение профилей"),
            "log": ("Журнал", "Команды, ответы и сообщения диагностики"),
        }
        self._style()
        self._build_ui()
        self._populate_profiles()
        self._log("Запуск Bloody Control A70 v1.0.1.1")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._bind_shortcuts()
        self._schedule_worker_poll()

    def _load_profiles(self):
        """Load profiles defensively; normalize records from older versions."""
        APP_DIR.mkdir(parents=True, exist_ok=True)
        try:
            saved = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
            if isinstance(saved, list):
                good = []
                seen = set()
                for raw in saved:
                    if not isinstance(raw, dict):
                        continue
                    try:
                        name = str(raw.get("name", "")).strip()
                        level = int(raw.get("level", -1))
                        effect = str(raw.get("effect", "Статика"))
                        speed = max(1, min(10, int(raw.get("speed", 5))))
                        if not name or level not in LEVEL_NAMES or effect not in EFFECTS:
                            continue
                        key = name.casefold()
                        if key in seen:
                            continue
                        dpi = int(raw.get("dpi", 1000))
                        if dpi not in DPI_VALUES:
                            dpi = 1000
                        sequence_raw = raw.get("sequence", [0, 1, 2, 3, 2, 1])
                        if effect == "Свой цикл":
                            if not isinstance(sequence_raw, list):
                                sequence_raw = parse_level_sequence(str(sequence_raw))
                            sequence = parse_level_sequence(",".join(str(x) for x in sequence_raw))
                        else:
                            sequence = [0, 1, 2, 3, 2, 1]
                        good.append({"name": name, "level": level, "effect": effect, "speed": speed, "sequence": sequence, "dpi": dpi})
                        seen.add(key)
                    except (ValueError, TypeError):
                        continue
                if good:
                    return good
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        defaults = [
            {"name": "Ночь", "level": 0, "effect": "Статика", "speed": 5, "sequence": [0, 1, 2, 3, 2, 1], "dpi": 1000},
            {"name": "Тускло", "level": 1, "effect": "Статика", "speed": 5, "sequence": [0, 1, 2, 3, 2, 1], "dpi": 1000},
            {"name": "Обычный", "level": 2, "effect": "Статика", "speed": 5, "sequence": [0, 1, 2, 3, 2, 1], "dpi": 1000},
            {"name": "Ярко", "level": 3, "effect": "Статика", "speed": 5, "sequence": [0, 1, 2, 3, 2, 1], "dpi": 1600},
            {"name": "Дыхание", "level": 2, "effect": "Дыхание", "speed": 4, "sequence": [0, 1, 2, 3, 2, 1], "dpi": 1000},
        ]
        try:
            self._write_profile_file(defaults)
        except OSError:
            pass
        return defaults

    def _write_profile_file(self, profiles):
        APP_DIR.mkdir(parents=True, exist_ok=True)
        temp_file = PROFILES_FILE.with_suffix(".json.tmp")
        temp_file.write_text(json.dumps(profiles, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_file.replace(PROFILES_FILE)

    def _save_profiles(self):
        self._write_profile_file(self.profiles)

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL_2, foreground=MUTED,
                        padding=(14, 9), font=("Segoe UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", PURPLE)],
                  foreground=[("selected", "#ffffff")])
        style.configure("Vertical.TScrollbar", background=PANEL_2, troughcolor=BG,
                        bordercolor=BG, arrowcolor=MUTED, width=11)
        style.configure("TCombobox", fieldbackground=PANEL_2, background=PANEL_2,
                        foreground=TEXT, arrowcolor=TEAL, bordercolor=BORDER,
                        lightcolor=BORDER, darkcolor=BORDER, focuscolor=PURPLE,
                        borderwidth=0, padding=6, font=("Segoe UI", 10), relief="flat")
        style.map("TCombobox", fieldbackground=[("readonly", PANEL_2), ("focus", PANEL_2)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", PANEL_2)],
                  selectforeground=[("readonly", TEXT)],
                  bordercolor=[("focus", PURPLE), ("!focus", BORDER)],
                  lightcolor=[("focus", PURPLE), ("!focus", BORDER)],
                  darkcolor=[("focus", PURPLE), ("!focus", BORDER)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=30, borderwidth=0,
                        font=("Segoe UI", 9))
        style.configure("Treeview.Heading", background=PANEL_2, foreground=TEXT,
                        font=("Segoe UI", 9, "bold"), padding=7)
        style.map("Treeview", background=[("selected", PURPLE)])

    def _build_ui(self):
        """New sidebar/dashboard layout. Keeps the proven HID backend and all existing handlers."""
        self.root.configure(bg=BG)
        shell = tk.Frame(self.root, bg=BG)
        shell.pack(fill="both", expand=True)

        # Left navigation rail
        sidebar = tk.Frame(shell, bg=SIDEBAR, width=205, highlightbackground="#1D2435", highlightthickness=1)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        logo_row = tk.Frame(sidebar, bg=SIDEBAR)
        logo_row.pack(fill="x", padx=17, pady=(23, 23))
        logo = tk.Label(logo_row, text="B", font=("Segoe UI", 22, "bold"),
                        bg=PURPLE, fg="#ffffff", width=2, height=1, padx=2, pady=4)
        logo.pack(side="left", padx=(0, 10))
        logo_text = tk.Frame(logo_row, bg=SIDEBAR)
        logo_text.pack(side="left", fill="x", expand=True)
        tk.Label(logo_text, text="BLOODY", font=("Segoe UI", 12, "bold"),
                 bg=SIDEBAR, fg=TEXT).pack(anchor="w")
        tk.Label(logo_text, text="CONTROL  ·  A70", font=("Segoe UI", 7, "bold"),
                 bg=SIDEBAR, fg=TEAL).pack(anchor="w", pady=(2, 0))

        tk.Label(sidebar, text="РАБОЧАЯ ОБЛАСТЬ", font=("Segoe UI", 8, "bold"),
                 bg=SIDEBAR, fg="#69758F").pack(anchor="w", padx=20, pady=(0, 9))
        nav_specs = [
            ("overview", "⌂", "Главная"),
            ("light", "✦", "Подсветка"),
            ("dpi", "◎", "DPI / Сенсор"),
            ("profiles", "▤", "Профили"),
            ("device", "⌁", "Устройство"),
            ("automation", "◷", "Автоматизация"),
            ("log", "≡", "Журнал"),
        ]
        for key, glyph, label in nav_specs:
            row = tk.Frame(sidebar, bg=SIDEBAR)
            row.pack(fill="x", padx=9, pady=2)
            indicator = tk.Frame(row, bg=SIDEBAR, width=3)
            indicator.pack(side="left", fill="y", padx=(0, 5))
            btn = tk.Button(row, text=f"{glyph}   {label}", anchor="w", command=lambda k=key: self.show_page(k),
                            bg=SIDEBAR, fg=MUTED, activebackground=PANEL_2, activeforeground=TEXT,
                            relief="flat", bd=0, highlightthickness=0, borderwidth=0,
                            highlightbackground=SIDEBAR, padx=12, pady=11, cursor="hand2",
                            font=("Segoe UI", 10, "bold"))
            btn.pack(side="left", fill="x", expand=True)
            btn.bind("<Enter>", lambda e, b=btn, k=key: b.configure(bg=PANEL_2 if self.active_page_key == k else "#171D2B"), add="+")
            btn.bind("<Leave>", lambda e, b=btn, k=key: b.configure(bg=PANEL_2 if self.active_page_key == k else SIDEBAR), add="+")
            self.nav_items[key] = (row, btn, indicator)

        # Compact status panel pinned to sidebar bottom
        sidebar_bottom = tk.Frame(sidebar, bg="#151B29", highlightbackground=BORDER, highlightthickness=1)
        sidebar_bottom.pack(side="bottom", fill="x", padx=11, pady=14)
        status_head = tk.Frame(sidebar_bottom, bg="#151B29")
        status_head.pack(fill="x", padx=11, pady=(10, 3))
        self.sidebar_dot = tk.Label(status_head, text="●", font=("Segoe UI", 10, "bold"),
                                    bg="#151B29", fg=AMBER)
        self.sidebar_dot.pack(side="left", padx=(0, 6))
        tk.Label(status_head, text="USB DEVICE", font=("Segoe UI", 8, "bold"),
                 bg="#151B29", fg=MUTED).pack(side="left")
        self.sidebar_status = tk.Label(sidebar_bottom, textvariable=self.connected_var,
                                       font=("Segoe UI", 8), bg="#151B29", fg=TEXT,
                                       justify="left", wraplength=160, anchor="w")
        self.sidebar_status.pack(fill="x", padx=11, pady=(0, 11))
        tk.Label(sidebar, text="Unofficial companion utility", font=("Segoe UI", 8),
                 bg=SIDEBAR, fg="#59647A").pack(side="bottom", pady=(0, 3))
        tk.Label(sidebar, text="v1.0  ·  A70", font=("Segoe UI", 8, "bold"),
                 bg=SIDEBAR, fg="#59647A").pack(side="bottom", pady=(0, 2))

        # Main workspace
        workspace = tk.Frame(shell, bg=BG)
        workspace.pack(side="left", fill="both", expand=True)
        topbar = tk.Frame(workspace, bg=BG)
        topbar.pack(fill="x", padx=25, pady=(19, 15))
        title_box = tk.Frame(topbar, bg=BG)
        title_box.pack(side="left", fill="x", expand=True)
        self.page_title_var = tk.StringVar(value="Главная")
        self.page_subtitle_var = tk.StringVar(value="Состояние мыши и быстрые действия")
        tk.Label(title_box, textvariable=self.page_title_var, font=("Segoe UI", 22, "bold"),
                 bg=BG, fg=TEXT).pack(anchor="w")
        tk.Label(title_box, textvariable=self.page_subtitle_var, font=("Segoe UI", 9),
                 bg=BG, fg=MUTED).pack(anchor="w", pady=(3, 0))
        right_controls = tk.Frame(topbar, bg=BG)
        right_controls.pack(side="right")
        self.connection_label = tk.Label(right_controls, textvariable=self.connected_var,
                                         font=("Segoe UI", 9, "bold"), bg=PANEL_2,
                                         fg=MUTED, padx=11, pady=8)
        self.connection_label.pack(side="left", padx=(0, 9))
        self.connect_btn = self._button(right_controls, "Найти A70  ↗", self.connect_mouse, main=True)
        self.connect_btn.pack(side="left")

        # Page stack
        self.page_container = tk.Frame(workspace, bg=BG)
        self.page_container.pack(fill="both", expand=True, padx=24, pady=(0, 8))
        self.overview_tab = tk.Frame(self.page_container, bg=BG)
        self.light_tab = tk.Frame(self.page_container, bg=BG)
        self.dpi_tab = tk.Frame(self.page_container, bg=BG)
        self.profile_tab = tk.Frame(self.page_container, bg=BG)
        self.device_tab = tk.Frame(self.page_container, bg=BG)
        self.automation_tab = tk.Frame(self.page_container, bg=BG)
        self.log_tab = tk.Frame(self.page_container, bg=BG)
        self.pages = {
            "overview": self.overview_tab, "light": self.light_tab, "dpi": self.dpi_tab,
            "profiles": self.profile_tab, "device": self.device_tab,
            "automation": self.automation_tab, "log": self.log_tab,
        }
        self._build_overview_tab()
        self._build_light_tab()
        self._build_dpi_tab()
        self._build_profile_tab()
        self._build_device_tab()
        self._build_automation_tab()
        self._build_log_tab()

        footer = tk.Frame(workspace, bg="#111722", highlightbackground=BORDER, highlightthickness=1)
        footer.pack(fill="x", padx=24, pady=(0, 12))
        self.status_dot = tk.Label(footer, text="●", bg="#111722", fg=TEAL,
                                   font=("Segoe UI", 8, "bold"))
        self.status_dot.pack(side="left", padx=(11, 7), pady=8)
        self.status_label = tk.Label(footer, textvariable=self.status_var, bg="#111722", fg=self.status_color,
                                     font=("Segoe UI", 9), anchor="w", justify="left",
                                     wraplength=850, padx=0, pady=8)
        self.status_label.pack(side="left", fill="x", expand=True)
        self.root.bind("<Configure>", self._resize_status_wrap, add="+")
        self.root.bind_all("<MouseWheel>", self._scroll_active_tab, add="+")
        self.root.bind_all("<Button-4>", lambda e: self._scroll_active_tab_linux(-1), add="+")
        self.root.bind_all("<Button-5>", lambda e: self._scroll_active_tab_linux(1), add="+")
        self.show_page("overview")

    def show_page(self, key):
        if key not in self.pages:
            return
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.active_page_key = key
        self.active_page_frame = self.pages[key]
        title, subtitle = self.page_meta.get(key, ("Bloody Control", ""))
        self.page_title_var.set(title)
        self.page_subtitle_var.set(subtitle)
        for name, (_row, btn, indicator) in self.nav_items.items():
            active = name == key
            btn.configure(bg=PANEL_2 if active else SIDEBAR,
                          fg=TEXT if active else MUTED)
            indicator.configure(bg=PURPLE if active else SIDEBAR)
        self._log(f"Открыта страница: {title}.")

    def _resize_status_wrap(self, event):
        if event.widget is self.root and hasattr(self, "status_label"):
            self.status_label.configure(wraplength=max(420, event.width - 64))

    def _scroll_host(self, tab):
        """Create a vertically scrollable tab body so content never gets cut off."""
        outer = tk.Frame(tab, bg=BG)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, bg=BG, highlightthickness=0, bd=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                                 bg=PANEL_2, troughcolor=BG, activebackground=PURPLE,
                                 highlightbackground=BG, highlightcolor=BG,
                                 bd=0, relief="flat", width=11)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = tk.Frame(canvas, bg=BG)
        window_id = canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _e, c=canvas: c.configure(scrollregion=c.bbox("all")))
        canvas.bind("<Configure>", lambda e, c=canvas, w=window_id: c.itemconfigure(w, width=e.width))
        self.scroll_canvases[str(tab)] = canvas
        return content

    def _scroll_active_tab(self, event):
        if not hasattr(self, "active_page_frame") or self.active_page_frame is None:
            return
        canvas = self.scroll_canvases.get(str(self.active_page_frame))
        if canvas is None:
            return
        delta = getattr(event, "delta", 0)
        if delta:
            canvas.yview_scroll(int(-delta / 120) or (-1 if delta > 0 else 1), "units")

    def _scroll_active_tab_linux(self, direction):
        if not hasattr(self, "active_page_frame") or self.active_page_frame is None:
            return
        canvas = self.scroll_canvases.get(str(self.active_page_frame))
        if canvas is not None:
            canvas.yview_scroll(direction, "units")

    def _card(self, parent, title, subtitle=None):
        # Card keeps its children as direct children because legacy tab builders pack into it.
        frame = tk.Frame(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        accent = tk.Frame(frame, bg=PURPLE, width=4)
        accent.pack(side="left", fill="y")
        tk.Label(frame, text=title, font=("Segoe UI", 13, "bold"),
                 bg=PANEL, fg=TEXT).pack(anchor="w", padx=18, pady=(14, 4))
        if subtitle:
            subtitle_label = tk.Label(frame, text=subtitle, font=("Segoe UI", 9), bg=PANEL,
                                      fg=MUTED, wraplength=700, justify="left", anchor="w")
            subtitle_label.pack(anchor="w", fill="x", padx=18, pady=(0, 12))
            frame.bind("<Configure>", lambda e, lbl=subtitle_label: lbl.configure(
                wraplength=max(220, min(720, e.width - 50)) if e.width > 0 else 600)
            )
        return frame

    def _button(self, parent, text, command, main=False, danger=False):
        bg = PURPLE if main else ("#542637" if danger else PANEL_2)
        active = PURPLE_HOVER if main else ("#74334B" if danger else PANEL_3)
        fg = "#ffffff" if main or danger else TEXT
        b = tk.Button(parent, text=text, command=command, bg=bg, fg=fg,
                      activebackground=active, activeforeground="#ffffff", relief="flat", bd=0,
                      font=("Segoe UI", 9, "bold"), padx=13, pady=10, cursor="hand2",
                      highlightthickness=0)
        b.bind("<Enter>", lambda _e: b.configure(bg=active), add="+")
        b.bind("<Leave>", lambda _e: b.configure(bg=bg), add="+")
        return b

    def _build_overview_tab(self):
        wrap = self._scroll_host(self.overview_tab)
        hero = tk.Frame(wrap, bg="#171D2B", highlightbackground="#2E3650", highlightthickness=1)
        hero.pack(fill="x", pady=(2, 13))
        bar = tk.Frame(hero, bg=PURPLE, width=5)
        bar.pack(side="left", fill="y")
        hero_content = tk.Frame(hero, bg="#171D2B")
        hero_content.pack(side="left", fill="both", expand=True, padx=22, pady=20)
        hero_top = tk.Frame(hero_content, bg="#171D2B")
        hero_top.pack(fill="x")
        tk.Label(hero_top, text="ТВОЯ МЫШЬ. ТВОИ НАСТРОЙКИ.", font=("Segoe UI", 9, "bold"),
                 bg="#171D2B", fg=TEAL).pack(anchor="w")
        tk.Label(hero_content, text="Bloody A70 Control Center", font=("Segoe UI", 21, "bold"),
                 bg="#171D2B", fg=TEXT).pack(anchor="w", pady=(7, 4))
        tk.Label(hero_content, text="Управляй подсветкой, текущей чувствительностью и профилями в одном месте.",
                 font=("Segoe UI", 10), bg="#171D2B", fg=MUTED, wraplength=680,
                 justify="left").pack(anchor="w")
        hero_actions = tk.Frame(hero_content, bg="#171D2B")
        hero_actions.pack(fill="x", pady=(17, 0))
        self._button(hero_actions, "Открыть DPI  →", lambda: self.show_page("dpi"), main=True).pack(side="left", padx=(0, 9))
        self._button(hero_actions, "Настроить подсветку", lambda: self.show_page("light")).pack(side="left")

        grid = tk.Frame(wrap, bg=BG)
        grid.pack(fill="x", pady=(0, 12))
        grid.grid_columnconfigure(0, weight=1, uniform="overview")
        grid.grid_columnconfigure(1, weight=1, uniform="overview")
        dpi_card = tk.Frame(grid, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        dpi_card.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        tk.Label(dpi_card, text="CURRENT DPI", font=("Segoe UI", 9, "bold"), bg=PANEL, fg=MUTED).pack(anchor="w", padx=18, pady=(16, 2))
        dpi_row = tk.Frame(dpi_card, bg=PANEL)
        dpi_row.pack(fill="x", padx=18, pady=(0, 2))
        tk.Label(dpi_row, textvariable=self.dpi_current_value_var, font=("Segoe UI", 31, "bold"),
                 bg=PANEL, fg=TEAL).pack(side="left")
        tk.Label(dpi_row, text="DPI", font=("Segoe UI", 12, "bold"), bg=PANEL, fg=MUTED).pack(side="left", padx=(8, 0), pady=(13, 0))
        tk.Label(dpi_card, textvariable=self.dpi_current_stage_var, font=("Segoe UI", 9), bg=PANEL, fg=MUTED,
                 wraplength=350, justify="left").pack(anchor="w", padx=18, pady=(0, 14))
        self._button(dpi_card, "Изменить DPI", lambda: self.show_page("dpi"), main=True).pack(anchor="w", padx=18, pady=(0, 17))

        light_card = tk.Frame(grid, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        light_card.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        tk.Label(light_card, text="BACKLIGHT", font=("Segoe UI", 9, "bold"), bg=PANEL, fg=MUTED).pack(anchor="w", padx=18, pady=(16, 3))
        tk.Label(light_card, textvariable=self.brightness_info_var, font=("Segoe UI", 15, "bold"), bg=PANEL, fg=TEXT,
                 wraplength=350, justify="left").pack(anchor="w", padx=18, pady=(3, 15))
        light_actions = tk.Frame(light_card, bg=PANEL)
        light_actions.pack(fill="x", padx=18, pady=(0, 17))
        self._button(light_actions, "Выключить", lambda: self.apply_level(0, persist=True)).pack(side="left", padx=(0, 8))
        self._button(light_actions, "Максимум", lambda: self.apply_level(3, persist=True), main=True).pack(side="left")

        quick = self._card(wrap, "Быстрые настройки DPI", "Выбери значение, нажми применить и проверь ответ мыши. Текущее значение отображается выше.")
        quick.pack(fill="x", pady=(0, 12))
        quick_row = tk.Frame(quick, bg=PANEL)
        quick_row.pack(fill="x", padx=18, pady=(0, 17))
        for dpi in DPI_VALUES:
            self._button(quick_row, f"{dpi:,}".replace(",", " "), lambda v=dpi: self.apply_dpi(v), main=(dpi == self.dpi_selected_var.get())).pack(side="left", fill="x", expand=True, padx=(0, 7))

        connect_card = self._card(wrap, "С чего начать", "Подключи мышь и считай данные. После подключения DPI и яркость появятся в карточках выше.")
        connect_card.pack(fill="x")
        row = tk.Frame(connect_card, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(0, 16))
        self._button(row, "Найти / переподключить A70", self.connect_mouse, main=True).pack(side="left", padx=(0, 8))
        self._button(row, "Обновить DPI", lambda: self.refresh_dpi_slot()).pack(side="left", padx=(0, 8))
        self._button(row, "Информация об устройстве", lambda: self.show_page("device")).pack(side="left")

    def _build_dpi_tab(self):
        wrap = self._scroll_host(self.dpi_tab)
        current = tk.Frame(wrap, bg="#171D2B", highlightbackground="#2E3650", highlightthickness=1)
        current.pack(fill="x", pady=(2, 12))
        tk.Frame(current, bg=TEAL, width=5).pack(side="left", fill="y")
        body = tk.Frame(current, bg="#171D2B")
        body.pack(side="left", fill="both", expand=True, padx=22, pady=18)
        tk.Label(body, text="СЕЙЧАС АКТИВНО", font=("Segoe UI", 9, "bold"), bg="#171D2B", fg=TEAL).pack(anchor="w")
        number_line = tk.Frame(body, bg="#171D2B")
        number_line.pack(anchor="w", pady=(1, 0))
        tk.Label(number_line, textvariable=self.dpi_current_value_var, font=("Segoe UI", 42, "bold"),
                 bg="#171D2B", fg=TEXT).pack(side="left")
        tk.Label(number_line, text="DPI", font=("Segoe UI", 14, "bold"), bg="#171D2B", fg=MUTED).pack(side="left", padx=(10, 0), pady=(20, 0))
        tk.Label(body, textvariable=self.dpi_current_stage_var, font=("Segoe UI", 10), bg="#171D2B", fg=MUTED,
                 wraplength=750, justify="left").pack(anchor="w", pady=(2, 10))
        tk.Label(body, textvariable=self.dpi_protocol_status_var, font=("Segoe UI", 9, "bold"), bg="#171D2B", fg=AMBER,
                 wraplength=750, justify="left").pack(anchor="w")
        action = tk.Frame(body, bg="#171D2B")
        action.pack(anchor="w", pady=(12, 0))
        self._button(action, "Прочитать текущий DPI", lambda: self.refresh_dpi_slot(), main=True).pack(side="left", padx=(0, 8))
        self._button(action, "Открыть диагностику", lambda: self.show_page("device")).pack(side="left")

        choices = self._card(wrap, "Выбери DPI", "Пять ступеней для сенсора PMW3327 из Sensitivity.log: 1000 / 1200 / 1600 / 2000 / 6200 DPI. После применения программа проверяет активную ступень по ответу мыши.")
        choices.pack(fill="x", pady=(0, 12))
        tk.Label(choices, text="БЫСТРЫЕ ЗНАЧЕНИЯ", font=("Segoe UI", 8, "bold"), bg=PANEL, fg=MUTED).pack(anchor="w", padx=18, pady=(0, 9))
        self.dpi_buttons = {}
        dpi_grid = tk.Frame(choices, bg=PANEL)
        dpi_grid.pack(fill="x", padx=18, pady=(0, 12))
        for i, dpi in enumerate(DPI_VALUES):
            col = tk.Frame(dpi_grid, bg=PANEL_2, highlightbackground=BORDER, highlightthickness=1)
            col.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else 5, 0 if i == len(DPI_VALUES)-1 else 5))
            dpi_grid.grid_columnconfigure(i, weight=1, uniform="dpi-presets")
            btn = tk.Button(col, text=f"{dpi:,}".replace(",", " "), command=lambda v=dpi: self.select_dpi(v),
                            bg=PANEL_2, fg=TEXT, activebackground=PANEL_3, activeforeground="#ffffff",
                            relief="flat", bd=0, font=("Segoe UI", 13, "bold"), padx=10, pady=15,
                            cursor="hand2", highlightthickness=0)
            btn.pack(fill="x", expand=True)
            self.dpi_buttons[dpi] = (col, btn)
        tk.Label(choices, text="Выбор значения не меняет мышь автоматически. Нажми кнопку ниже, чтобы применить его.",
                 font=("Segoe UI", 9), bg=PANEL, fg=MUTED, wraplength=720, justify="left").pack(anchor="w", padx=18, pady=(0, 14))
        control_row = tk.Frame(choices, bg=PANEL)
        control_row.pack(fill="x", padx=18, pady=(0, 17))
        self.dpi_selected_text_var = tk.StringVar(value="Выбрано: 1000 DPI")
        tk.Label(control_row, textvariable=self.dpi_selected_text_var, font=("Segoe UI", 11, "bold"), bg=PANEL, fg=TEXT).pack(side="left", padx=(0, 16))
        self._button(control_row, "Применить выбранный DPI", lambda: self.apply_dpi(int(self.dpi_selected_var.get())), main=True).pack(side="left", padx=(0, 8))
        self._button(control_row, "Перечитать", self.refresh_dpi_slot).pack(side="left")
        self.select_dpi(int(self.dpi_selected_var.get()))

        note = self._card(wrap, "Проверка результата", "После применения программа читает ответ мыши и показывает, совпала ли активная ступень. Если ответ не подтверждает изменение, используй аппаратную кнопку DPI на мыши и повторно считай значение.")
        note.pack(fill="x")
        tk.Label(note, text="Числовое значение зависит от таблицы DPI, сохранённой в штатном Workshop.",
                 font=("Segoe UI", 9), bg=PANEL, fg=MUTED, wraplength=750, justify="left").pack(anchor="w", padx=18, pady=(0, 16))

    def select_dpi(self, dpi):
        if dpi not in DPI_REGISTER_MAP:
            return
        self.dpi_selected_var.set(int(dpi))
        if hasattr(self, "dpi_selected_text_var"):
            self.dpi_selected_text_var.set(f"Выбрано: {int(dpi):,}".replace(",", " ") + " DPI")
        for value, (frame, button) in getattr(self, "dpi_buttons", {}).items():
            selected = value == int(dpi)
            frame.configure(bg="#28213B" if selected else PANEL_2,
                            highlightbackground=PURPLE if selected else BORDER)
            button.configure(bg=PURPLE if selected else PANEL_2,
                             activebackground=PURPLE_HOVER if selected else PANEL_3,
                             fg="#ffffff" if selected else TEXT)


    def _build_light_tab(self):
        wrap = self._scroll_host(self.light_tab)
        top = self._card(wrap, "Управление подсветкой", "Эти элементы меняют яркость самой мыши через HID. A70 подтверждает четыре аппаратных уровня; это не произвольный RGB.")
        top.pack(fill="x", pady=(2, 12))
        row = tk.Frame(top, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(2, 16))
        self.current_swatch = tk.Canvas(row, width=94, height=74, bg=PANEL_2, highlightthickness=0, bd=0)
        self.current_swatch.pack(side="left", padx=(0, 15))
        self.current_swatch.create_oval(16, 10, 78, 72, fill="#B184FF", outline="")
        detail = tk.Frame(row, bg=PANEL)
        detail.pack(side="left", fill="x", expand=True)
        tk.Label(detail, textvariable=self.brightness_info_var, font=("Segoe UI", 15, "bold"), bg=PANEL, fg=TEXT).pack(anchor="w", pady=(5, 4))
        tk.Label(detail, text="Выбери уровень и примени. Программа проверит значение обратным чтением.", font=("Segoe UI", 9), bg=PANEL, fg=MUTED, wraplength=650, justify="left").pack(anchor="w")

        level_card = self._card(wrap, "Уровень яркости", "Быстрые сочетания Ctrl+1…Ctrl+4 доступны, когда окно программы активно.")
        level_card.pack(fill="x", pady=(0, 12))
        tk.Scale(level_card, from_=0, to=3, resolution=1, orient="horizontal", variable=self.level_var,
                 command=self._level_slider_changed, showvalue=False, bg=PANEL, fg=TEXT, troughcolor=PANEL_2,
                 activebackground=PURPLE, highlightthickness=0, bd=0, relief="flat").pack(fill="x", padx=18, pady=(5, 3))
        labels = tk.Frame(level_card, bg=PANEL)
        labels.pack(fill="x", padx=18, pady=(0, 10))
        for i, name in LEVEL_NAMES.items():
            tk.Label(labels, text=f"{i}: {name}", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", expand=True)
        actions = tk.Frame(level_card, bg=PANEL)
        actions.pack(fill="x", padx=18, pady=(0, 16))
        action_specs = [
            ("Применить и сохранить", self.apply_selected_level, True),
            ("Применить временно", lambda: self.apply_level(int(self.level_var.get()), persist=False), False),
            ("Прочитать с мыши", self.refresh_brightness, False),
            ("Выключить", lambda: self.apply_level(0, persist=True), False),
            ("Максимум", lambda: self.apply_level(3, persist=True), False),
            ("Тест 0 → 3", self.test_brightness_levels, False),
        ]
        for i, (label, callback, primary) in enumerate(action_specs):
            self._button(actions, label, callback, main=primary).grid(row=i // 3, column=i % 3, sticky="ew", padx=(0, 8), pady=(0, 7))
            actions.grid_columnconfigure(i % 3, weight=1)

        effect_card = self._card(wrap, "Световые эффекты", "Программные эффекты меняют реальные уровни яркости. Цветовые RGB-команды не имитируются.")
        effect_card.pack(fill="x", pady=(0, 12))
        erow = tk.Frame(effect_card, bg=PANEL)
        erow.pack(fill="x", padx=18, pady=(0, 9))
        tk.Label(erow, text="Режим", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 12))
        self.effect_combo = ttk.Combobox(erow, textvariable=self.effect_var, values=list(EFFECTS), state="readonly", width=25)
        self.effect_combo.pack(side="left", padx=(0, 14), ipady=3)
        tk.Label(erow, text="Скорость", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 8))
        tk.Scale(erow, from_=1, to=10, resolution=1, orient="horizontal", variable=self.speed_var, showvalue=True,
                 bg=PANEL, fg=TEXT, troughcolor=PANEL_2, activebackground=PURPLE, highlightthickness=0, bd=0, length=180).pack(side="left")
        erow2 = tk.Frame(effect_card, bg=PANEL)
        erow2.pack(fill="x", padx=18, pady=(0, 12))
        self._button(erow2, "Запустить эффект", self.start_selected_effect, main=True).pack(side="left", padx=(0, 8))
        self._button(erow2, "Остановить эффект", self.stop_effect, danger=True).pack(side="left", padx=(0, 12))
        tk.Label(erow2, text="Уровни 0–3", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(side="right")
        seqrow = tk.Frame(effect_card, bg=PANEL)
        seqrow.pack(fill="x", padx=18, pady=(0, 10))
        tk.Label(seqrow, text="Свой цикл:", bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        tk.Entry(seqrow, textvariable=self.custom_sequence_var, bg=PANEL_2, fg=TEXT, insertbackground=TEXT,
                 relief="flat", width=26, font=("Segoe UI", 10)).pack(side="left", ipady=5)
        tk.Label(seqrow, text="0,1,2,3,2,1", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(side="left", padx=10)
        durrow = tk.Frame(effect_card, bg=PANEL)
        durrow.pack(fill="x", padx=18, pady=(0, 15))
        tk.Label(durrow, text="Остановить через:", bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))
        ttk.Combobox(durrow, textvariable=self.effect_duration_var, state="readonly", width=18,
                     values=["Без ограничения", "10 секунд", "30 секунд", "1 минута", "5 минут"]).pack(side="left", ipady=3)

        timer_card = self._card(wrap, "Автоотключение", "Таймер выключает подсветку и сохраняет состояние OFF.")
        timer_card.pack(fill="x")
        trow = tk.Frame(timer_card, bg=PANEL)
        trow.pack(fill="x", padx=18, pady=(0, 15))
        tk.Label(trow, text="Выключить через", bg=PANEL, fg=TEXT, font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 10))
        self.auto_off_combo = ttk.Combobox(trow, textvariable=self.auto_off_var, state="readonly", width=16,
                                           values=["Не отключать", "5 минут", "15 минут", "30 минут", "60 минут"])
        self.auto_off_combo.pack(side="left", padx=(0, 8), ipady=3)
        self._button(trow, "Запустить таймер", self.start_auto_off, main=True).pack(side="left", padx=(0, 8))
        self._button(trow, "Отменить таймер", self.cancel_auto_off).pack(side="left")

    def _build_profile_tab(self):
        # Keep the three most-used actions pinned in view, even on short laptop screens.
        action_bar = tk.Frame(self.profile_tab, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        action_bar.pack(side="bottom", fill="x", padx=2, pady=(4, 3))
        for col in range(3):
            action_bar.grid_columnconfigure(col, weight=1, uniform="profile-actions")
        self._button(action_bar, "＋  Создать профиль", self.create_profile, main=True).grid(row=0, column=0, sticky="ew", padx=(8, 4), pady=7)
        self._button(action_bar, "Обновить выбранный", self.update_profile).grid(row=0, column=1, sticky="ew", padx=4, pady=7)
        self._button(action_bar, "Применить к мыши", self.apply_profile, main=True).grid(row=0, column=2, sticky="ew", padx=(4, 8), pady=7)
        host = self._scroll_host(self.profile_tab)
        left = tk.Frame(host, bg=BG)
        left.pack(side="left", fill="both", expand=True, padx=(2, 8), pady=2)
        right = tk.Frame(host, bg=BG, width=330)
        right.pack(side="right", fill="y", padx=(6, 2), pady=2)
        list_card = self._card(left, "Мои профили", "Сколько угодно собственных профилей. Профиль можно применить к мыши одним нажатием.")
        list_card.pack(fill="both", expand=True)
        self.profile_list = tk.Listbox(list_card, bg=PANEL_2, fg=TEXT, selectbackground=PURPLE, selectforeground="#ffffff",
                                       font=("Segoe UI", 11), relief="flat", borderwidth=0, highlightthickness=0,
                                       activestyle="none", height=16)
        self.profile_list.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.profile_list.bind("<<ListboxSelect>>", self._on_profile_select)
        self.profile_list.bind("<Double-Button-1>", lambda _e: self.apply_profile())
        editor = self._card(right, "Параметры профиля", "Профиль сохраняет яркость и программный эффект.")
        editor.pack(fill="x", pady=(0, 10))
        self.profile_name_var = tk.StringVar(value="Мой профиль")
        self.profile_level_var = tk.IntVar(value=2)
        self.profile_effect_var = tk.StringVar(value="Статика")
        self.profile_speed_var = tk.IntVar(value=5)
        self.profile_dpi_var = tk.IntVar(value=1000)
        tk.Label(editor, text="Название", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(10, 3))
        tk.Entry(editor, textvariable=self.profile_name_var, bg=PANEL_2, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER, highlightcolor=PURPLE, font=("Segoe UI", 10)).pack(fill="x", padx=18, ipady=7)
        tk.Label(editor, text="Яркость", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(12, 3))
        ttk.Combobox(editor, textvariable=self.profile_level_var, values=[0, 1, 2, 3], state="readonly").pack(fill="x", padx=18, ipady=3)
        tk.Label(editor, text="Эффект", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(12, 3))
        ttk.Combobox(editor, textvariable=self.profile_effect_var, values=list(EFFECTS), state="readonly").pack(fill="x", padx=18, ipady=3)
        tk.Label(editor, text="DPI", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(12, 3))
        ttk.Combobox(editor, textvariable=self.profile_dpi_var, values=DPI_VALUES, state="readonly").pack(fill="x", padx=18, ipady=3)
        tk.Label(editor, text="Скорость эффекта", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(12, 3))
        tk.Scale(editor, from_=1, to=10, resolution=1, orient="horizontal", variable=self.profile_speed_var,
                 bg=PANEL, fg=TEXT, troughcolor=PANEL_2, activebackground=PURPLE, highlightthickness=0, bd=0).pack(fill="x", padx=12)
        tk.Label(editor, text="Свой цикл уровней (если выбран «Свой цикл»)", bg=PANEL, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=18, pady=(8, 3))
        tk.Entry(editor, textvariable=self.profile_sequence_var, bg=PANEL_2, fg=TEXT, insertbackground=TEXT, relief="flat", bd=0, highlightthickness=1, highlightbackground=BORDER, highlightcolor=PURPLE, font=("Segoe UI", 9)).pack(fill="x", padx=18, ipady=5)
        tk.Label(editor, text="Создание, обновление и применение закреплены снизу окна.",
                 bg=PANEL, fg=MUTED, font=("Segoe UI", 9), wraplength=260, justify="left").pack(anchor="w", padx=18, pady=(10, 6))
        self._button(editor, "Дублировать выбранный", self.duplicate_profile).pack(fill="x", padx=18, pady=4)
        self._button(editor, "Удалить выбранный", self.delete_profile, danger=True).pack(fill="x", padx=18, pady=4)
        self._button(editor, "Экспорт профиля…", self.export_profile).pack(fill="x", padx=18, pady=(8, 4))
        self._button(editor, "Импорт профиля…", self.import_profile).pack(fill="x", padx=18, pady=(0, 16))


    def _build_automation_tab(self):
        wrap = self._scroll_host(self.automation_tab)
        rotate = self._card(wrap, "Автопереключение профилей", "Программа по очереди применяет сохранённые профили к мыши. Работает с текущей библиотекой профилей и реальными командами яркости.")
        rotate.pack(fill="x", pady=(0, 10))
        row = tk.Frame(rotate, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(0, 10))
        tk.Label(row, text="Интервал:", bg=PANEL, fg=TEXT, font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 10))
        ttk.Combobox(row, textvariable=self.profile_rotation_interval_var, state="readonly", width=16,
                     values=["30 секунд", "1 минута", "5 минут", "10 минут"]).pack(side="left", padx=(0, 10), ipady=3)
        self._button(row, "Старт ротации", self.start_profile_rotation, main=True).pack(side="left", padx=(0, 8))
        self._button(row, "Остановить ротацию", self.stop_profile_rotation, danger=True).pack(side="left", padx=(0, 8))
        self._button(row, "Следующий профиль", self.apply_next_profile).pack(side="left")
        tk.Label(rotate, textvariable=self.profile_rotation_status_var, bg=PANEL, fg=MUTED, font=("Segoe UI", 10)).pack(anchor="w", padx=18, pady=(0, 16))

        shortcuts = self._card(wrap, "Быстрые действия", "На этой панели собраны быстрые действия для подсветки.")
        shortcuts.pack(fill="x", pady=(0, 10))
        grid = tk.Frame(shortcuts, bg=PANEL)
        grid.pack(fill="x", padx=18, pady=(0, 16))
        for i, (name, level) in enumerate((("Ночь / OFF", 0), ("Низкая", 1), ("Средняя", 2), ("Высокая", 3))):
            self._button(grid, name, lambda lv=level: self.apply_level(lv, persist=True), main=(level == 3)).grid(row=0, column=i, padx=(0, 8), sticky="ew")
            grid.grid_columnconfigure(i, weight=1)

        safety = self._card(wrap, "Что действительно управляется", "Аппаратно проверено пользователем: четыре уровня подсветки. Программные эффекты создаются последовательностью этих уровней.")
        safety.pack(fill="x")
        text = (
            "• 4 аппаратных уровня яркости, чтение и проверка после применения\n"
            "• Сохранение настройки в мышь или временное применение без сохранения\n"
            "• Дыхание, пульс, стробоскоп, случайное мерцание, двойной импульс, SOS и свой цикл\n"
            "• Автоотключение подсветки и автоостановка эффекта\n"
            "• Профили с импортом/экспортом и автоматической ротацией\n"
            "• USB/HID-диагностика, чтение яркости, попытка прочитать модель/прошивку и активный DPI-слот\n\n"
            "Числовой DPI использует пять ступеней профиля PMW3327 из Sensitivity.log. Изменение проверяется чтением активного DPI-слота мыши. RGB-цвета и переназначение кнопок в текущей версии не реализованы."
        )
        tk.Label(safety, text=text, bg=PANEL, fg=TEXT, font=("Segoe UI", 10), justify="left", wraplength=560).pack(anchor="w", padx=18, pady=(0, 16))

    def _build_device_tab(self):
        host = self._scroll_host(self.device_tab)
        info = self._card(host, "Состояние устройства", "Читаем данные с USB/HID и показываем, какой интерфейс реально используется.")
        info.pack(fill="x", padx=2, pady=(2, 10))
        tk.Label(info, textvariable=self.hardware_info_var, bg=PANEL, fg=TEXT, font=("Consolas", 10), justify="left", wraplength=560).pack(anchor="w", padx=18, pady=(4, 8))
        tk.Label(info, textvariable=self.dpi_info_var, bg=PANEL, fg=MUTED, font=("Segoe UI", 10), justify="left", wraplength=560).pack(anchor="w", padx=18, pady=(0, 12))
        row = tk.Frame(info, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(0, 16))
        self._button(row, "Прочитать модель / прошивку", self.refresh_device_info, main=True).pack(side="left", padx=(0, 8))
        self._button(row, "Проверить активный DPI-слот", self.refresh_dpi_slot).pack(side="left", padx=(0, 8))
        self._button(row, "Перечитать яркость", self.refresh_brightness).pack(side="left", padx=(0, 8))
        self._button(row, "Экспорт диагностики…", self.export_diagnostics).pack(side="left")

        hid_card = self._card(host, "Обнаруженные HID-интерфейсы", "Одна мышь часто отображается несколькими HID-записями. Запись для управления яркостью — та, в пути которой есть mi_02&col05.")
        hid_card.pack(fill="both", expand=True, padx=2, pady=(0, 2))
        cols = ("product", "interface", "usage", "path")
        self.hid_tree = ttk.Treeview(hid_card, columns=cols, show="headings", height=8)
        self.hid_tree.heading("product", text="Устройство")
        self.hid_tree.heading("interface", text="Интерфейс")
        self.hid_tree.heading("usage", text="Usage page / usage")
        self.hid_tree.heading("path", text="Путь HID (конец)")
        self.hid_tree.column("product", width=190, anchor="w")
        self.hid_tree.column("interface", width=80, anchor="center")
        self.hid_tree.column("usage", width=140, anchor="center")
        self.hid_tree.column("path", width=470, anchor="w")
        self.hid_tree.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        self._button(hid_card, "Обновить список интерфейсов", self.refresh_hid_inventory).pack(anchor="e", padx=18, pady=(0, 14))

    def _build_log_tab(self):
        host = self._scroll_host(self.log_tab)
        card = self._card(host, "Журнал событий", "В журнале отмечаются реальные команды, ответы и ошибки. Его можно сохранить и прислать для диагностики.")
        card.pack(fill="both", expand=True, padx=2, pady=2)
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=18, pady=(0, 8))
        self._button(row, "Сохранить журнал…", self.export_log).pack(side="left", padx=(0, 8))
        self._button(row, "Очистить журнал", self.clear_log).pack(side="left", padx=(0, 8))
        self._button(row, "Копировать журнал", self.copy_log).pack(side="left")
        self.log_text = tk.Text(card, bg="#0c0d12", fg="#c8cad6", insertbackground=TEXT, font=("Consolas", 9),
                                relief="flat", wrap="word", borderwidth=0, height=20)
        self.log_text.pack(fill="both", expand=True, padx=18, pady=(0, 18))
        self.log_text.configure(state="disabled")

    def _bind_shortcuts(self):
        for key, level in (("1", 0), ("2", 1), ("3", 2), ("4", 3)):
            self.root.bind_all(f"<Control-Key-{key}>", lambda e, lv=level: self.apply_level(lv, persist=True))
        self.root.bind_all("<Control-s>", lambda e: self.save_current_as_profile())

    def _log(self, text: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-1500:]
        if hasattr(self, "log_text"):
            self.log_text.configure(state="normal")
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")

    def set_status(self, text: str, color=MUTED):
        self.status_var.set(text)
        self.status_label.configure(fg=color)
        self._log(text)

    def _require_device(self) -> bool:
        if self.device is None:
            self.set_status("Сначала нажми «Найти A70» и подключи интерфейс.", AMBER)
            messagebox.showwarning(APP_TITLE, "Сначала найди и подключи Bloody A70.")
            return False
        return True

    def _enumerate(self):
        import hid
        self.hid = hid
        entries = self.hid.enumerate(VID, PID)
        self.hid_entries = entries
        return entries

    def connect_mouse(self):
        """Start HID discovery in a daemon worker so no native HID call can freeze Tk."""
        if self._connect_pending:
            return
        self._connect_generation += 1
        generation = self._connect_generation
        self._connect_pending = True
        self.connect_btn.configure(state="disabled", text="Подключение…")
        self.connected_var.set("Поиск интерфейса мыши…")
        self.connection_label.configure(fg=AMBER)
        self.set_status("Ищу Bloody A70 в фоне. Интерфейс останется доступным.", MUTED)
        worker = threading.Thread(target=self._connect_worker, args=(generation,), daemon=True,
                                  name=f"BloodyConnect-{generation}")
        worker.start()
        self._connect_timeout_id = self.root.after(8000, lambda g=generation: self._connect_timeout(g))

    def _connect_worker(self, generation):
        """Worker thread only: never touches Tk widgets or Tk variables."""
        entries = []
        try:
            import hid
            entries = list(hid.enumerate(VID, PID))
            candidates = [e for e in entries if TARGET_PATH_MARKER in path_text(e.get("path", "")).lower()]
            if not candidates:
                self._worker_queue.put({"kind": "connect_result", "generation": generation,
                                        "entries": entries, "error": None,
                                        "message": f"Найдено HID-записей: {len(entries)}, но нужный интерфейс mi_02&col05 не найден."})
                return
            errors = []
            for entry in candidates:
                dev = None
                try:
                    dev = hid.device()
                    dev.open_path(entry["path"])
                    # The known working read is kept; if a driver call stalls, only this daemon thread waits.
                    current = read_brightness(dev)
                    self._worker_queue.put({"kind": "connect_result", "generation": generation,
                                            "entries": entries, "device": dev, "entry": entry,
                                            "level": current, "error": None})
                    return
                except Exception as exc:
                    errors.append(str(exc))
                    if dev is not None:
                        try:
                            dev.close()
                        except Exception:
                            pass
            self._worker_queue.put({"kind": "connect_result", "generation": generation,
                                    "entries": entries, "error": "; ".join(errors) or "Нет ответа от интерфейса."})
        except Exception as exc:
            self._worker_queue.put({"kind": "connect_result", "generation": generation,
                                    "entries": entries, "error": str(exc)})

    def _connect_timeout(self, generation):
        if generation != self._connect_generation or not self._connect_pending:
            return
        # Invalidate late results. The native call is isolated in a daemon thread.
        self._connect_generation += 1
        self._connect_pending = False
        self._connect_timeout_id = None
        self.connect_btn.configure(state="normal", text="Повторить подключение")
        self.connected_var.set("Ответ от USB/HID задерживается")
        self.connection_label.configure(fg=AMBER)
        self.set_status("HID не ответил за 8 секунд. Окно не заблокировано; закрой Workshop и попробуй ещё раз.", AMBER)

    def _handle_connect_result(self, result):
        generation = result.get("generation")
        dev = result.get("device")
        if generation != self._connect_generation or not self._connect_pending:
            if dev is not None:
                try:
                    threading.Thread(target=dev.close, daemon=True).start()
                except Exception:
                    pass
            return
        self._connect_pending = False
        if self._connect_timeout_id is not None:
            try:
                self.root.after_cancel(self._connect_timeout_id)
            except Exception:
                pass
            self._connect_timeout_id = None
        self.connect_btn.configure(state="normal", text="Переподключить" if dev else "Найти A70")
        entries = result.get("entries", [])
        self.hid_entries = entries
        self._render_hid_inventory(entries, set_status=False)
        if dev is None:
            self.connected_var.set("Не подключено")
            self.connection_label.configure(fg=MUTED)
            message = result.get("message") or ("Интерфейс найден, но мышь не ответила: " + str(result.get("error") or "нет данных"))
            self.set_status(message, AMBER if "не найден" in message.lower() else RED)
            return
        old_device = self.device
        self.device = dev
        self.device_entry = result.get("entry")
        self.current_level = int(result.get("level", 2))
        self.selected_static_level = self.current_level
        self.level_var.set(self.current_level)
        self._level_slider_changed()
        self.brightness_info_var.set(f"Текущая яркость: {LEVEL_NAMES[self.current_level]} · уровень {self.current_level}")
        self._update_swatch(self.current_level)
        self.device_info = None
        self.hardware_info_var.set("Мышь подключена. Нажми «Прочитать модель / прошивку», чтобы получить сведения.")
        self.dpi_current_value_var.set("—")
        self.dpi_current_stage_var.set("Нажми «Прочитать текущий DPI» для проверки активной ступени")
        self.dpi_info_var.set("DPI ещё не считывался")
        self.dpi_protocol_status_var.set("Подключение готово; чтение DPI выполняется только по запросу")
        self.connected_var.set("Подключено · mi_02&col05")
        self.connection_label.configure(fg=GREEN)
        if old_device is not None and old_device is not dev:
            try:
                threading.Thread(target=old_device.close, daemon=True).start()
            except Exception:
                pass
        name = (self.device_entry or {}).get("product_string") or "Bloody A Series"
        self._log(f"Подключено к HID {name}; яркость прочитана: {self.current_level} ({LEVEL_NAMES[self.current_level]}). Информация и DPI не запрашивались автоматически.")
        self.set_status(f"Мышь подключена. Текущая яркость: {LEVEL_NAMES[self.current_level].lower()}.", GREEN)

    def _schedule_worker_poll(self):
        if self._closing:
            return
        try:
            self._worker_poll_id = self.root.after(100, self._poll_worker_queue)
        except tk.TclError:
            self._worker_poll_id = None

    def _poll_worker_queue(self):
        if self._closing:
            return
        try:
            while True:
                result = self._worker_queue.get_nowait()
                kind = result.get("kind")
                if kind == "connect_result":
                    self._handle_connect_result(result)
                elif kind == "device_info_result":
                    self._handle_device_info_result(result)
                elif kind == "dpi_read_result":
                    self._handle_dpi_read_result(result)
                elif kind == "dpi_apply_result":
                    self._handle_dpi_apply_result(result)
                elif kind == "hid_inventory_result":
                    self._hid_inventory_pending = False
                    if "error" in result:
                        self.set_status(f"Не удалось получить список HID: {result['error']}", RED)
                    else:
                        self.hid_entries = result.get("entries", [])
                        self._render_hid_inventory(self.hid_entries)
        except queue.Empty:
            pass
        except Exception as exc:
            self._log(f"Ошибка обработки фоновой задачи: {exc}")
        self._schedule_worker_poll()

    def _begin_device_task(self, name):
        if self.device is None:
            self.set_status("Сначала подключи Bloody A70.", AMBER)
            return None
        if self._device_task_pending:
            self.set_status(f"Устройство занято задачей «{self._device_task_name}». Дождись ответа.", AMBER)
            return None
        self._device_task_generation += 1
        generation = self._device_task_generation
        self._device_task_pending = True
        self._device_task_name = name
        self.set_status(f"{name}: выполняется в фоне…", MUTED)
        self._device_task_timeout_id = self.root.after(7000, lambda g=generation: self._device_task_timeout(g))
        return generation

    def _device_task_timeout(self, generation):
        if generation != self._device_task_generation or not self._device_task_pending:
            return
        # Keep the device task locked to avoid overlapping commands while the HID driver is still busy.
        self._device_task_timeout_id = None
        self.set_status(f"«{self._device_task_name}» отвечает дольше обычного. Программа не зависла; не запускай параллельную команду к мыши.", AMBER)

    def _finish_device_task(self, generation):
        if generation != self._device_task_generation:
            return False
        self._device_task_pending = False
        self._device_task_name = ""
        if self._device_task_timeout_id is not None:
            try:
                self.root.after_cancel(self._device_task_timeout_id)
            except Exception:
                pass
            self._device_task_timeout_id = None
        return True

    def _level_slider_changed(self, *_):
        value = int(self.level_var.get())
        self.level_text_var.set(f"{LEVEL_NAMES[value]} · уровень {value}")
        if hasattr(self, "brightness_info_var") and self.current_level is None:
            self.brightness_info_var.set(f"Выбрано: {LEVEL_NAMES[value]} · ещё не применено")

    def refresh_brightness(self):
        if not self._require_device():
            return
        try:
            level = read_brightness(self.device)
            self.current_level = level
            self.selected_static_level = level if not self.effect_running else self.selected_static_level
            self.level_var.set(level if not self.effect_running else self.level_var.get())
            self.brightness_info_var.set(f"Текущая яркость: {LEVEL_NAMES[level]} · уровень {level}")
            self._update_swatch(level)
            self.set_status(f"Яркость прочитана с мыши: {LEVEL_NAMES[level].lower()}.", GREEN)
            return level
        except Exception as exc:
            self.set_status(f"Не удалось прочитать яркость: {exc}", RED)
            return None

    def _update_swatch(self, level):
        if not hasattr(self, "current_swatch"):
            return
        colors = {0: "#30323b", 1: "#65547a", 2: "#9870c9", 3: "#c4a3ff"}
        self.current_swatch.delete("all")
        self.current_swatch.create_oval(16, 10, 78, 72, fill=colors.get(level, PURPLE), outline="")

    def send_level_live(self, level: int):
        if self.device is None:
            raise RuntimeError("Мышь не подключена")
        send_feature(self.device, brightness_write(level), f"яркость {level}")
        self.current_level = level
        self._update_swatch(level)
        self.brightness_info_var.set(f"Яркость: {LEVEL_NAMES[level]} · уровень {level}")

    def apply_level(self, level: int, persist=True, show_status=True):
        level = int(level)
        if level not in LEVEL_NAMES:
            return
        if not self._require_device():
            return
        if self.level_test_running:
            self._cancel_level_test(restore=False)
        if self.effect_running:
            self._stop_effect_timer_only()
            self._cancel_effect_stop_timer()
            self.effect_running = False
        try:
            self.send_level_live(level)
            if persist:
                send_feature(self.device, packet(OP_PERSIST), "сохранение яркости")
                time.sleep(0.05)
            actual = read_brightness(self.device)
            self.current_level = actual
            self.level_var.set(actual)
            self.selected_static_level = actual
            self.brightness_info_var.set(f"Текущая яркость: {LEVEL_NAMES[actual]} · уровень {actual}")
            self._update_swatch(actual)
            if actual != level:
                self.set_status(f"Команда отправлена, но мышь вернула уровень {actual}, а просили {level}.", AMBER)
            elif show_status:
                suffix = " и настройка сохранена" if persist else " (временное изменение, без команды сохранения)"
                self.set_status(f"Мышь подтвердила: {LEVEL_NAMES[actual].lower()} яркость{suffix}.", GREEN)
            return actual
        except Exception as exc:
            self.set_status(f"Не удалось применить яркость: {exc}", RED)
            messagebox.showerror("Ошибка управления", f"Изменение не подтверждено.\n\n{exc}")
            return None

    def apply_selected_level(self):
        self.apply_level(int(self.level_var.get()), persist=True)

    def start_selected_effect(self):
        if not self._require_device():
            return
        effect = self.effect_var.get()
        if effect == "Статика":
            self.apply_selected_level()
            return
        self.start_effect(effect, int(self.speed_var.get()))

    def start_effect(self, effect: str, speed: int):
        if not self._require_device():
            return
        if effect not in EFFECTS or effect == "Статика":
            self.apply_selected_level()
            return
        if effect == "Свой цикл":
            try:
                self.custom_sequence = parse_level_sequence(self.custom_sequence_var.get())
            except ValueError as exc:
                messagebox.showwarning(APP_TITLE, str(exc))
                return
        self._stop_effect_timer_only()
        self._cancel_effect_stop_timer()
        self.effect_name = effect
        self.effect_speed = max(1, min(10, int(speed)))
        self.effect_step = 0
        self.effect_running = True
        # Keep the slider value as the brightness to restore when the effect stops.
        self.selected_static_level = int(self.level_var.get())
        self._log(f"Запущен программный эффект «{effect}», скорость {self.effect_speed}/10. Команда persist не отправляется на каждом кадре.")
        self.set_status(f"Эффект «{effect}» работает на реальной подсветке. Нажми остановку, чтобы вернуть выбранную яркость.", GREEN)
        self._effect_tick()
        self._schedule_effect_stop()

    def _effect_frame(self):
        if self.effect_name == "Дыхание":
            seq = [0, 1, 2, 3, 2, 1]
            level = seq[self.effect_step % len(seq)]
            delay = int(1150 - (self.effect_speed - 1) * 95)
        elif self.effect_name == "Пульс":
            seq = [0, 3]
            level = seq[self.effect_step % len(seq)]
            delay = int(900 - (self.effect_speed - 1) * 75)
        elif self.effect_name == "Стробоскоп":
            seq = [0, 3]
            level = seq[self.effect_step % len(seq)]
            delay = int(280 - (self.effect_speed - 1) * 20)
        elif self.effect_name == "Случайное мерцание":
            level = random.randint(0, 3)
            delay = int(800 - (self.effect_speed - 1) * 65)
        elif self.effect_name == "Двойной импульс":
            seq = [0, 3, 0, 3, 0, 0, 0]
            level = seq[self.effect_step % len(seq)]
            delay = int(180 if level == 3 else 220 + (10 - self.effect_speed) * 35)
        elif self.effect_name == "SOS":
            # SOS: three short, three long, three short, with quiet pauses.
            seq = [3,0,3,0,3,0, 3,3,3,0,3,3,3,0,3,3,3,0, 3,0,3,0,3,0,0,0,0]
            idx = self.effect_step % len(seq)
            level = seq[idx]
            delay = int((180 if idx < 6 or idx >= 18 else 520) + (10 - self.effect_speed) * 20)
        elif self.effect_name == "Свой цикл":
            seq = self.custom_sequence or [0, 1, 2, 3]
            level = seq[self.effect_step % len(seq)]
            delay = int(1000 - (self.effect_speed - 1) * 90)
        else:
            level = self.selected_static_level
            delay = 1000
        return level, max(90, delay)

    def _effect_tick(self):
        if not self.effect_running or self.device is None:
            return
        level, delay = self._effect_frame()
        try:
            self.send_level_live(level)  # live frame: no persistence write on every step
            self.effect_step += 1
            self.status_var.set(f"Эффект «{self.effect_name}» · кадр яркости {level}/3 · скорость {self.effect_speed}/10")
            self.status_label.configure(fg=GREEN)
            self.effect_after_id = self.root.after(delay, self._effect_tick)
        except Exception as exc:
            self._stop_effect_timer_only()
            self.effect_running = False
            self.set_status(f"Эффект остановлен из-за ошибки HID: {exc}", RED)

    def _stop_effect_timer_only(self):
        if self.effect_after_id is not None:
            try:
                self.root.after_cancel(self.effect_after_id)
            except Exception:
                pass
            self.effect_after_id = None

    def stop_effect(self):
        was_running = self.effect_running
        self._stop_effect_timer_only()
        self._cancel_effect_stop_timer()
        self.effect_running = False
        if was_running and self.device is not None:
            self.apply_level(self.selected_static_level, persist=True)
        elif was_running:
            self.set_status("Эффект остановлен, но мышь отключена — яркость не восстановлена.", AMBER)
        else:
            self.set_status("Эффект не запущен.", MUTED)


    def _schedule_effect_stop(self):
        self._cancel_effect_stop_timer()
        choice = self.effect_duration_var.get()
        seconds = {"10 секунд": 10, "30 секунд": 30, "1 минута": 60, "5 минут": 300}.get(choice)
        if seconds is not None and self.effect_running:
            self.effect_stop_after_id = self.root.after(seconds * 1000, self._effect_duration_done)
            self._log(f"Автоостановка эффекта назначена через {choice}.")

    def _effect_duration_done(self):
        self.effect_stop_after_id = None
        if self.effect_running:
            self._log("Время эффекта истекло; возвращаю выбранную яркость.")
            self.stop_effect()

    def _cancel_effect_stop_timer(self):
        if self.effect_stop_after_id is not None:
            try:
                self.root.after_cancel(self.effect_stop_after_id)
            except Exception:
                pass
            self.effect_stop_after_id = None

    def test_brightness_levels(self):
        if self.level_test_running:
            self._cancel_level_test(restore=True)
            self.set_status("Тест яркости остановлен, возвращён исходный уровень.", MUTED)
            return
        if not self._require_device():
            return
        if self.effect_running:
            self.stop_effect()
        try:
            self.level_test_restore = read_brightness(self.device)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Не удалось прочитать исходную яркость:\n{exc}")
            return
        self.level_test_running = True
        self.level_test_index = 0
        self.set_status("Тест яркости: последовательно проверяю уровни 0, 1, 2, 3.", GREEN)
        self._level_test_tick()

    def _level_test_tick(self):
        if not self.level_test_running or self.device is None:
            self._cancel_level_test(restore=False)
            return
        sequence = [0, 1, 2, 3]
        if self.level_test_index >= len(sequence):
            self._cancel_level_test(restore=True)
            self.set_status("Тест завершён. Исходный уровень восстановлен и сохранён.", GREEN)
            return
        level = sequence[self.level_test_index]
        try:
            self.send_level_live(level)
            self._log(f"Тест уровней: установлен уровень {level} без сохранения каждого шага.")
            self.level_test_index += 1
            self.level_test_after_id = self.root.after(650, self._level_test_tick)
        except Exception as exc:
            self._cancel_level_test(restore=True)
            self.set_status(f"Тест остановлен из-за ошибки HID: {exc}", RED)

    def _cancel_level_test(self, restore=False):
        if self.level_test_after_id is not None:
            try:
                self.root.after_cancel(self.level_test_after_id)
            except Exception:
                pass
            self.level_test_after_id = None
        was_running = self.level_test_running
        self.level_test_running = False
        if restore and was_running and self.device is not None:
            try:
                send_feature(self.device, brightness_write(self.level_test_restore), "восстановление яркости после теста")
                send_feature(self.device, packet(OP_PERSIST), "сохранение исходной яркости после теста")
                actual = read_brightness(self.device)
                self.current_level = actual
                self.level_var.set(actual)
                self.selected_static_level = actual
                self.brightness_info_var.set(f"Текущая яркость: {LEVEL_NAMES[actual]} · уровень {actual}")
                self._update_swatch(actual)
            except Exception as exc:
                self._log(f"Не удалось восстановить яркость после теста: {exc}")

    def start_auto_off(self):
        choice = self.auto_off_var.get()
        minutes = {"5 минут": 5, "15 минут": 15, "30 минут": 30, "60 минут": 60}.get(choice)
        self.cancel_auto_off(silent=True)
        if minutes is None:
            self.set_status("Автоотключение не задано.", MUTED)
            return
        self.auto_off_deadline = time.time() + minutes * 60
        self.auto_off_after_id = self.root.after(minutes * 60 * 1000, self._auto_off_fire)
        self._auto_off_countdown()
        self.set_status(f"Подсветка будет выключена через {minutes} минут.", GREEN)

    def _auto_off_countdown(self):
        if self.auto_off_deadline is None:
            return
        left = max(0, int(self.auto_off_deadline - time.time()))
        self.auto_off_status_var.set(f"Осталось {left // 60:02d}:{left % 60:02d}")
        if left > 0:
            self.auto_off_tick_id = self.root.after(1000, self._auto_off_countdown)

    def _auto_off_fire(self):
        self.auto_off_after_id = None
        self.auto_off_deadline = None
        if self.auto_off_tick_id is not None:
            try: self.root.after_cancel(self.auto_off_tick_id)
            except Exception: pass
            self.auto_off_tick_id = None
        self.auto_off_status_var.set("Таймер не активен")
        self._cancel_effect_stop_timer()
        if self.effect_running:
            self._stop_effect_timer_only()
            self.effect_running = False
        if self.device is not None:
            self.apply_level(0, persist=True)
            self.set_status("Автоотключение: подсветка выключена и сохранена.", GREEN)
        else:
            self.set_status("Сработал таймер, но мышь сейчас не подключена.", AMBER)

    def apply_dpi(self, dpi, from_profile=False, show_status=True):
        """Apply DPI on a worker thread to keep the interface responsive."""
        try:
            dpi = int(dpi)
        except (TypeError, ValueError):
            return False
        if dpi not in DPI_REGISTER_MAP:
            messagebox.showwarning(APP_TITLE, "Выбери одно из значений A70: 1000, 1200, 1600, 2000 или 6200 DPI.")
            return False
        generation = self._begin_device_task(f"Применение {dpi} DPI")
        if generation is None:
            return False
        device = self.device
        self.dpi_info_var.set(f"Отправляю настройку {dpi} DPI и проверяю ответ мыши…")
        self.dpi_current_stage_var.set("Команда выполняется в фоне")
        worker = threading.Thread(target=self._dpi_apply_worker,
                                  args=(generation, device, dpi, bool(from_profile), bool(show_status)),
                                  daemon=True, name=f"BloodyDPI-{generation}")
        worker.start()
        return True

    def _dpi_apply_worker(self, generation, device, dpi, from_profile, show_status):
        result = {"kind": "dpi_apply_result", "generation": generation, "dpi": dpi,
                  "from_profile": from_profile, "show_status": show_status}
        try:
            old_slot, old_raw = read_current_dpi_slot(device)
            rate_code = int(old_raw[11]) if old_raw and len(old_raw) > 11 and int(old_raw[11]) in DPI_RATE_HZ else 0x02
            a, r = DPI_REGISTER_MAP[dpi]
            payload = bytearray(REPORT_LEN)
            payload[0] = REPORT_ID
            payload[1] = 0x0D
            payload[8] = len(DPI_VALUES) - 1
            payload[9] = DPI_VALUES.index(dpi)
            payload[10] = a
            payload[11] = a
            payload[12] = 0x80 | r
            payload[13] = rate_code
            send_feature(device, bytes(payload), f"применение {dpi} DPI")
            time.sleep(0.06)
            verify_slot, verify_raw = read_current_dpi_slot(device)
            result.update({"ok": True, "old_slot": old_slot, "old_raw": old_raw,
                           "verify_slot": verify_slot, "verify_raw": verify_raw,
                           "rate_code": rate_code, "register_a": a, "register_r": r})
        except Exception as exc:
            result.update({"ok": False, "error": str(exc)})
        self._worker_queue.put(result)

    def _handle_dpi_apply_result(self, result):
        generation = result.get("generation")
        if not self._finish_device_task(generation):
            return
        dpi = int(result.get("dpi", 1000))
        if not result.get("ok"):
            error = result.get("error", "неизвестная ошибка")
            self.dpi_info_var.set(f"Не удалось применить DPI: {error}")
            self.dpi_current_stage_var.set("Ответ на изменение DPI не получен")
            if result.get("show_status", True):
                self.set_status(f"Не удалось изменить DPI: {error}", RED)
            self._log(f"Ошибка изменения DPI: {error}")
            return
        verify_slot = result.get("verify_slot")
        verify_raw = result.get("verify_raw")
        rate_code = result.get("rate_code")
        self.dpi_selected_var.set(dpi)
        self.select_dpi(dpi)
        self._log(f"DPI command sent: dpi={dpi}, index={DPI_VALUES.index(dpi)}, a=0x{result.get('register_a',0):02X}, r={result.get('register_r')}, rate={rate_code}; old_slot={result.get('old_slot')}, verified_slot={verify_slot}; raw={hex_head(verify_raw or [], 24)}")
        if verify_slot == DPI_VALUES.index(dpi):
            self.current_dpi_slot = verify_slot
            self.dpi_current_value_var.set(f"{dpi:,}".replace(",", " "))
            self.dpi_current_stage_var.set(f"Активная ступень {verify_slot + 1} из {len(DPI_VALUES)} · подтверждено мышью")
            self.dpi_info_var.set(f"DPI изменён на {dpi} и подтверждён ответом мыши.")
            if result.get("show_status", True):
                self.set_status(f"DPI изменён на {dpi} и подтверждён ответом мыши.", GREEN)
        else:
            self.dpi_info_var.set("Команда отправлена, но мышь не подтвердила выбранную ступень. Проверь скорость курсора и текущий DPI.")
            self.dpi_current_stage_var.set("Ответ мыши не подтвердил выбранное значение")
            if result.get("show_status", True):
                self.set_status("Команда DPI отправлена, но результат не подтверждён чтением мыши.", AMBER)

    def cancel_auto_off(self, silent=False):
        for attr in ("auto_off_after_id", "auto_off_tick_id"):
            task_id = getattr(self, attr)
            if task_id is not None:
                try: self.root.after_cancel(task_id)
                except Exception: pass
                setattr(self, attr, None)
        self.auto_off_deadline = None
        if hasattr(self, "auto_off_status_var"):
            self.auto_off_status_var.set("Таймер не активен")
        if not silent:
            self.set_status("Таймер автоотключения отменён.", MUTED)

    def refresh_device_info(self, silent=False):
        if self.device is None:
            if not silent:
                self.set_status("Сначала подключи Bloody A70.", AMBER)
            return
        generation = self._begin_device_task("Чтение модели и прошивки")
        if generation is None:
            return
        device = self.device
        self.hardware_info_var.set("Читаю информацию в фоне…")
        threading.Thread(target=self._device_info_worker, args=(generation, device, bool(silent)),
                         daemon=True, name=f"BloodyInfo-{generation}").start()

    def _device_info_worker(self, generation, device, silent):
        result = {"kind": "device_info_result", "generation": generation, "silent": silent}
        try:
            result["info"] = read_device_info(device)
        except Exception as exc:
            result["error"] = str(exc)
        self._worker_queue.put(result)

    def _handle_device_info_result(self, result):
        if not self._finish_device_task(result.get("generation")):
            return
        info = result.get("info")
        if info:
            self.device_info = info
            self.hardware_info_var.set(
                f"VID:PID = {VID:04X}:{PID:04X}\n"
                f"Модель (код протокола): 0x{info['model']:04X} · subtype {info['subtype']}\n"
                f"Версия прошивки: 0x{info['firmware']:02X} ({info['firmware']}) · flags 0x{info['flags']:02X}\n"
                f"Ответ HID: {hex_head(info['raw'], 18)}"
            )
            self.dpi_protocol_status_var.set(f"Модель 0x{info['model']:04X} и версия прошивки прочитаны.")
            self._log(f"Прочитана информация устройства: model=0x{info['model']:04X}, subtype={info['subtype']}, firmware=0x{info['firmware']:02X}.")
            if not result.get("silent"):
                self.set_status("Информация о модели и прошивке прочитана.", GREEN)
        else:
            error = result.get("error", "нет ответа")
            self.hardware_info_var.set(f"VID:PID = {VID:04X}:{PID:04X}\nКоманда информации не вернула данные.\n{error}")
            self.dpi_protocol_status_var.set("Сведения о модели не получены; возможность чтения DPI зависит от ответа мыши.")
            self._log(f"Информация устройства не прочитана: {error}")
            if not result.get("silent"):
                self.set_status("Не удалось прочитать модель/прошивку; остальные команды остаются доступны.", AMBER)

    def refresh_dpi_slot(self, silent=False):
        if self.device is None:
            if not silent:
                self.set_status("Сначала подключи Bloody A70.", AMBER)
            return
        generation = self._begin_device_task("Чтение текущего DPI")
        if generation is None:
            return
        device = self.device
        self.dpi_current_stage_var.set("Читаю активную ступень DPI в фоне…")
        self.dpi_info_var.set("Ожидаю ответ мыши…")
        threading.Thread(target=self._dpi_read_worker, args=(generation, device, bool(silent)),
                         daemon=True, name=f"BloodyDPIRead-{generation}").start()

    def _dpi_read_worker(self, generation, device, silent):
        result = {"kind": "dpi_read_result", "generation": generation, "silent": silent}
        try:
            result["slot"], result["raw"] = read_current_dpi_slot(device)
        except Exception as exc:
            result["error"] = str(exc)
        self._worker_queue.put(result)

    def _handle_dpi_read_result(self, result):
        if not self._finish_device_task(result.get("generation")):
            return
        if "error" in result:
            error = result["error"]
            self.dpi_current_value_var.set("—")
            self.dpi_current_stage_var.set("Не удалось прочитать DPI")
            self.dpi_info_var.set(f"Чтение DPI не удалось: {error}")
            self.dpi_protocol_status_var.set("Ответ мыши не получен.")
            if not result.get("silent"):
                self.set_status("Чтение DPI не удалось; интерфейс продолжает работать.", AMBER)
            return
        slot = result.get("slot")
        raw = result.get("raw") or []
        self.last_report = raw
        self.current_dpi_slot = slot
        if slot is not None and 0 <= slot < len(DPI_VALUES):
            dpi = DPI_VALUES[slot]
            self.dpi_current_value_var.set(f"{dpi:,}".replace(",", " "))
            self.dpi_current_stage_var.set(f"Активная ступень {slot + 1} из {len(DPI_VALUES)} · индекс {slot}")
            self.dpi_info_var.set(f"Мышь сообщила активную ступень {slot + 1}; значение по таблице Workshop: {dpi} DPI.")
            self.dpi_selected_var.set(dpi)
            self.select_dpi(dpi)
            rate = raw[11] if len(raw) > 11 else None
            rate_note = f" · опрос {DPI_RATE_HZ[rate]} Hz" if rate in DPI_RATE_HZ else ""
            self.dpi_protocol_status_var.set(f"Прочитан ответ мыши{rate_note}.")
            self._log(f"DPI slot response: slot={slot}, mapped_dpi={dpi}; raw={hex_head(raw, 24)}")
            if not result.get("silent"):
                self.set_status(f"Текущий DPI прочитан: {dpi}.", GREEN)
        elif slot is not None:
            self.dpi_current_value_var.set("?")
            self.dpi_current_stage_var.set(f"Мышь сообщила неизвестную ступень {slot}")
            self.dpi_info_var.set(f"Прочитан активный слот {slot}, для которого нет числового значения в таблице.")
            self.dpi_protocol_status_var.set("Ступень прочитана; сопоставление не найдено.")
            if not result.get("silent"):
                self.set_status("Прочитан DPI-слот без сопоставленного значения.", AMBER)
        else:
            self.dpi_current_value_var.set("—")
            self.dpi_current_stage_var.set("Мышь не вернула номер активной ступени DPI")
            self.dpi_info_var.set("Активный DPI-слот не получен. Текущая настройка мыши не изменялась.")
            self.dpi_protocol_status_var.set("Ответ с номером DPI-слота не получен.")
            if not result.get("silent"):
                self.set_status("Мышь не вернула активный DPI-слот.", AMBER)

    def _render_hid_inventory(self, entries, set_status=True):
        for item in self.hid_tree.get_children():
            self.hid_tree.delete(item)
        for entry in entries:
            path = path_text(entry.get("path", ""))
            marker = " ← КОМАНДЫ" if TARGET_PATH_MARKER in path.lower() else ""
            product = entry.get("product_string") or entry.get("manufacturer_string") or "Bloody A Series"
            iface = entry.get("interface_number", "?")
            usage = f"{int(entry.get('usage_page') or 0):04X} / {int(entry.get('usage') or 0):04X}"
            self.hid_tree.insert("", "end", values=(product, f"{iface}{marker}", usage, path[-76:]))
        self._log(f"HID-диагностика: найдено {len(entries)} записей VID_{VID:04X}/PID_{PID:04X}; целевой интерфейс помечен стрелкой.")
        if set_status:
            self.set_status(f"Обновлён список HID: {len(entries)} записей.", GREEN)

    def refresh_hid_inventory(self):
        """Enumeration is done in background as well; Windows HID APIs can occasionally stall."""
        if getattr(self, "_hid_inventory_pending", False):
            self.set_status("Список HID уже обновляется.", AMBER)
            return
        self._hid_inventory_pending = True
        def worker():
            try:
                import hid
                entries = list(hid.enumerate(VID, PID))
                self._worker_queue.put({"kind": "hid_inventory_result", "entries": entries})
            except Exception as exc:
                self._worker_queue.put({"kind": "hid_inventory_result", "error": str(exc)})
        threading.Thread(target=worker, daemon=True, name="BloodyHIDInventory").start()

    def _populate_profiles(self, selected_index=0):
        """Refresh the visible list while preserving the intended selection/editor state."""
        self.profile_list.delete(0, "end")
        for p in self.profiles:
            level = int(p.get("level", 2))
            self.profile_list.insert("end", f"{p['name']}   ·   {LEVEL_NAMES.get(level, 'Неизвестно')}   ·   {p.get('dpi', 1000)} DPI   ·   {p.get('effect', 'Статика')}")
        if self.profiles:
            selected_index = max(0, min(int(selected_index), len(self.profiles) - 1))
            self.profile_list.selection_clear(0, "end")
            self.profile_list.selection_set(selected_index)
            self.profile_list.activate(selected_index)
            self.profile_list.see(selected_index)
            self._on_profile_select()

    def _selected_profile_index(self):
        selected = self.profile_list.curselection()
        return int(selected[0]) if selected else None

    def _on_profile_select(self, _event=None):
        idx = self._selected_profile_index()
        if idx is None or idx >= len(self.profiles):
            return
        p = self.profiles[idx]
        self.profile_name_var.set(p.get("name", "Мой профиль"))
        self.profile_level_var.set(int(p.get("level", 2)))
        self.profile_effect_var.set(p.get("effect", "Статика"))
        self.profile_speed_var.set(int(p.get("speed", 5)))
        self.profile_dpi_var.set(int(p.get("dpi", 1000)))
        self.profile_sequence_var.set(",".join(str(x) for x in p.get("sequence", [0, 1, 2, 3, 2, 1])))

    def _profile_from_editor(self):
        name = self.profile_name_var.get().strip()
        if not name:
            raise ValueError("Введи название профиля.")
        level = int(self.profile_level_var.get())
        effect = self.profile_effect_var.get()
        speed = max(1, min(10, int(self.profile_speed_var.get())))
        if level not in LEVEL_NAMES or effect not in EFFECTS:
            raise ValueError("Некорректные параметры профиля.")
        sequence = parse_level_sequence(self.profile_sequence_var.get()) if effect == "Свой цикл" else [0, 1, 2, 3, 2, 1]
        dpi = int(self.profile_dpi_var.get())
        if dpi not in DPI_VALUES:
            raise ValueError("Выбери DPI из списка доступных значений.")
        return {"name": name, "level": level, "effect": effect, "speed": speed, "sequence": sequence, "dpi": dpi}

    def create_profile(self):
        try:
            new = self._profile_from_editor()
        except (ValueError, TypeError) as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        if any(p["name"].casefold() == new["name"].casefold() for p in self.profiles):
            messagebox.showwarning(APP_TITLE, "Профиль с таким именем уже существует. Измени имя или выбери «Обновить выбранный».")
            return
        self.profiles.append(new)
        try:
            self._save_profiles()
        except OSError as exc:
            self.profiles.pop()
            messagebox.showerror(APP_TITLE, f"Не удалось сохранить профили на диск:\n{exc}")
            return
        self._populate_profiles(selected_index=len(self.profiles) - 1)
        self.set_status(f"Создан и сохранён профиль «{new['name']}».", GREEN)

    def update_profile(self):
        idx = self._selected_profile_index()
        if idx is None:
            messagebox.showinfo(APP_TITLE, "Выбери профиль в списке.")
            return
        try:
            new = self._profile_from_editor()
        except (ValueError, TypeError) as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return
        if any(i != idx and p["name"].casefold() == new["name"].casefold() for i, p in enumerate(self.profiles)):
            messagebox.showwarning(APP_TITLE, "Другое имя уже занято.")
            return
        previous = self.profiles[idx]
        self.profiles[idx] = new
        try:
            self._save_profiles()
        except OSError as exc:
            self.profiles[idx] = previous
            messagebox.showerror(APP_TITLE, f"Не удалось сохранить изменения:\n{exc}")
            return
        self._populate_profiles(selected_index=idx)
        self.set_status(f"Профиль «{new['name']}» обновлён и сохранён.", GREEN)

    def _rotation_interval_ms(self):
        return {"30 секунд": 30_000, "1 минута": 60_000, "5 минут": 300_000, "10 минут": 600_000}.get(self.profile_rotation_interval_var.get(), 60_000)

    def start_profile_rotation(self):
        if not self._require_device():
            return
        if len(self.profiles) < 2:
            messagebox.showinfo(APP_TITLE, "Для ротации нужно хотя бы два сохранённых профиля.")
            return
        self.stop_profile_rotation(silent=True)
        idx = self._selected_profile_index()
        self.profile_rotation_index = (idx - 1) if idx is not None else -1
        self.profile_rotation_active = True
        self.profile_rotation_status_var.set("Ротация запущена")
        self._log("Запущена автоматическая ротация профилей.")
        self._profile_rotation_tick()

    def _profile_rotation_tick(self):
        if not self.profile_rotation_active or not self.profiles:
            return
        self.profile_rotation_index = (self.profile_rotation_index + 1) % len(self.profiles)
        idx = self.profile_rotation_index
        self.profile_list.selection_clear(0, "end")
        self.profile_list.selection_set(idx)
        self.profile_list.see(idx)
        self._on_profile_select()
        name = self.profiles[idx].get("name", f"Профиль {idx+1}")
        self.apply_profile(switch_tab=False)
        self.profile_rotation_status_var.set(f"Активен: {name} · следующий через {self.profile_rotation_interval_var.get()}")
        self.profile_rotation_after_id = self.root.after(self._rotation_interval_ms(), self._profile_rotation_tick)

    def stop_profile_rotation(self, silent=False):
        self.profile_rotation_active = False
        if self.profile_rotation_after_id is not None:
            try:
                self.root.after_cancel(self.profile_rotation_after_id)
            except Exception:
                pass
            self.profile_rotation_after_id = None
        self.profile_rotation_status_var.set("Автопереключение выключено")
        if not silent:
            self.set_status("Автопереключение профилей остановлено.", MUTED)

    def apply_next_profile(self):
        if not self._require_device():
            return
        if not self.profiles:
            return
        idx = self._selected_profile_index()
        next_idx = ((idx if idx is not None else -1) + 1) % len(self.profiles)
        self.profile_list.selection_clear(0, "end")
        self.profile_list.selection_set(next_idx)
        self.profile_list.see(next_idx)
        self._on_profile_select()
        self.apply_profile(switch_tab=False)
        self.set_status(f"Применён следующий профиль: {self.profiles[next_idx]['name']}.", GREEN)

    def apply_profile(self, switch_tab=True):
        idx = self._selected_profile_index()
        if idx is None:
            messagebox.showinfo(APP_TITLE, "Сначала выбери профиль.")
            return
        if not self._require_device():
            return
        p = self.profiles[idx]
        self.level_var.set(int(p["level"]))
        requested_dpi = int(p.get("dpi", 1000))
        self.dpi_selected_var.set(requested_dpi)
        self.select_dpi(requested_dpi)
        dpi_applied = self.apply_dpi(requested_dpi, from_profile=True, show_status=False)
        self.effect_var.set(p.get("effect", "Статика"))
        self.speed_var.set(int(p.get("speed", 5)))
        seq = p.get("sequence", [0, 1, 2, 3, 2, 1])
        self.profile_sequence_var.set(",".join(str(x) for x in seq))
        self.custom_sequence_var.set(",".join(str(x) for x in seq))
        self.selected_static_level = int(p["level"])
        if p.get("effect", "Статика") == "Статика":
            result = self.apply_level(int(p["level"]), persist=True, show_status=False)
            if result is None:
                return
        else:
            self.start_effect(p["effect"], int(p.get("speed", 5)))
            if not self.effect_running:
                return
        if switch_tab:
            self.show_page("light")
        self.set_status(f"Профиль «{p['name']}» применён к мыши.", GREEN)
        self._log(f"Применён профиль «{p['name']}»: яркость {p['level']}, эффект {p.get('effect', 'Статика')}, скорость {p.get('speed', 5)}.")

    def duplicate_profile(self):
        idx = self._selected_profile_index()
        if idx is None:
            return
        p = self.profiles[idx].copy()
        p["name"] = p["name"] + " — копия"
        suffix = 2
        original = p["name"]
        while any(x["name"].casefold() == p["name"].casefold() for x in self.profiles):
            p["name"] = f"{original} {suffix}"; suffix += 1
        self.profiles.append(p)
        try:
            self._save_profiles()
        except OSError as exc:
            self.profiles.pop()
            messagebox.showerror(APP_TITLE, f"Не удалось сохранить копию профиля:\n{exc}")
            return
        self._populate_profiles(selected_index=len(self.profiles) - 1)
        self.set_status(f"Создана копия профиля «{p['name']}».", GREEN)

    def delete_profile(self):
        idx = self._selected_profile_index()
        if idx is None:
            return
        name = self.profiles[idx]["name"]
        if not messagebox.askyesno(APP_TITLE, f"Удалить профиль «{name}»? Это не меняет настройки мыши."):
            return
        removed = self.profiles.pop(idx)
        was_last = not self.profiles
        if was_last:
            self.profiles = [{"name": "Обычный", "level": 2, "effect": "Статика", "speed": 5, "sequence": [0, 1, 2, 3, 2, 1]}]
        try:
            self._save_profiles()
        except OSError as exc:
            if was_last:
                self.profiles.clear()
            self.profiles.insert(idx, removed)
            messagebox.showerror(APP_TITLE, f"Не удалось сохранить список профилей:\n{exc}")
            self._populate_profiles(selected_index=idx)
            return
        self._populate_profiles(selected_index=min(idx, len(self.profiles) - 1))
        self.set_status(f"Профиль «{name}» удалён из локальной библиотеки.", GREEN)

    def save_current_as_profile(self):
        name = simpledialog.askstring(APP_TITLE, "Название нового профиля:", parent=self.root)
        if not name:
            return
        self.profile_name_var.set(name)
        self.profile_level_var.set(int(self.level_var.get()))
        self.profile_effect_var.set(self.effect_var.get())
        self.profile_speed_var.set(int(self.speed_var.get()))
        self.profile_dpi_var.set(int(self.dpi_selected_var.get()))
        self.profile_sequence_var.set(self.custom_sequence_var.get())
        self.create_profile()

    def export_profile(self):
        idx = self._selected_profile_index()
        if idx is None:
            return
        p = self.profiles[idx]
        dest = filedialog.asksaveasfilename(title="Экспорт профиля", defaultextension=".json",
                                            initialfile=p["name"].replace(" ", "_") + ".json",
                                            filetypes=[("JSON profile", "*.json")])
        if not dest:
            return
        Path(dest).write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")
        self.set_status(f"Профиль экспортирован: {dest}", GREEN)

    def import_profile(self):
        src = filedialog.askopenfilename(title="Импорт профиля", filetypes=[("JSON profile", "*.json"), ("All files", "*.*")])
        if not src:
            return
        try:
            p = json.loads(Path(src).read_text(encoding="utf-8"))
            if not isinstance(p, dict) or not str(p.get("name", "")).strip() or int(p.get("level", -1)) not in LEVEL_NAMES or p.get("effect", "Статика") not in EFFECTS:
                raise ValueError("В файле нет корректного профиля Bloody Control.")
            effect = p.get("effect", "Статика")
            sequence = parse_level_sequence(",".join(str(x) for x in p.get("sequence", [0, 1, 2, 3, 2, 1]))) if effect == "Свой цикл" else [0, 1, 2, 3, 2, 1]
            imported_dpi = int(p.get("dpi", 1000))
            if imported_dpi not in DPI_VALUES:
                imported_dpi = 1000
            p = {"name": str(p["name"]).strip(), "level": int(p["level"]), "effect": effect, "speed": max(1, min(10, int(p.get("speed", 5)))), "sequence": sequence, "dpi": imported_dpi}
            if any(x["name"].casefold() == p["name"].casefold() for x in self.profiles):
                p["name"] += " (импорт)"
            base_name = p["name"]
            suffix = 2
            while any(x["name"].casefold() == p["name"].casefold() for x in self.profiles):
                p["name"] = f"{base_name} ({suffix})"
                suffix += 1
            self.profiles.append(p)
            try:
                self._save_profiles()
            except OSError as exc:
                self.profiles.pop()
                raise OSError(f"не удалось сохранить профиль: {exc}")
            self._populate_profiles(selected_index=len(self.profiles) - 1)
            self.set_status(f"Импортирован профиль «{p['name']}».", GREEN)
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Не удалось импортировать профиль:\n{exc}")

    def export_diagnostics(self):
        try:
            entries = self._enumerate()
            rows = []
            for e in entries:
                path = path_text(e.get("path", ""))
                rows.append({
                    "product": e.get("product_string"),
                    "manufacturer": e.get("manufacturer_string"),
                    "vendor_id": e.get("vendor_id"), "product_id": e.get("product_id"),
                    "interface_number": e.get("interface_number"), "usage_page": e.get("usage_page"),
                    "usage": e.get("usage"), "path_tail": path[-100:],
                    "is_target_interface": TARGET_PATH_MARKER in path.lower(),
                })
        except Exception as exc:
            rows = [{"error": str(exc)}]
        report = {
            "app": APP_TITLE + " v0.5", "created": datetime.now().isoformat(timespec="seconds"),
            "target": {"VID": f"{VID:04X}", "PID": f"{PID:04X}", "interface_marker": TARGET_PATH_MARKER},
            "brightness_last_known": self.current_level,
            "device_info": ({k: v for k, v in self.device_info.items() if k != "raw"} | {"raw_hex": hex_head(self.device_info["raw"], 24)}) if self.device_info else None,
            "hid_entries": rows, "recent_log": self.log_lines[-150:],
            "note": "The report leaves out full HID paths and serial-like IDs; only path tails are included.",
        }
        dest = filedialog.asksaveasfilename(title="Экспорт диагностики", defaultextension=".json", initialfile="BloodyControl_diagnostics.json", filetypes=[("JSON", "*.json"), ("Text", "*.txt")])
        if not dest:
            return
        Path(dest).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self.set_status(f"Диагностика сохранена: {dest}", GREEN)

    def export_log(self):
        dest = filedialog.asksaveasfilename(title="Сохранить журнал", defaultextension=".txt", initialfile="BloodyControl_log.txt", filetypes=[("Text", "*.txt")])
        if dest:
            Path(dest).write_text("\n".join(self.log_lines) + "\n", encoding="utf-8")
            self.set_status(f"Журнал сохранён: {dest}", GREEN)

    def clear_log(self):
        self.log_lines = []
        self.log_text.configure(state="normal"); self.log_text.delete("1.0", "end"); self.log_text.configure(state="disabled")
        self.set_status("Журнал очищен.", MUTED)

    def copy_log(self):
        self.root.clipboard_clear(); self.root.clipboard_append("\n".join(self.log_lines)); self.set_status("Журнал скопирован в буфер обмена.", GREEN)

    def close(self):
        if self._closing:
            return
        self._closing = True
        try:
            self.cancel_auto_off(silent=True)
            self._cancel_effect_stop_timer()
            self._cancel_level_test(restore=False)
            self.stop_profile_rotation(silent=True)
            self._stop_effect_timer_only()
            self.effect_running = False
        except Exception:
            pass
        device = self.device
        self.device = None
        if device is not None:
            def close_device_handle(dev):
                try:
                    dev.close()
                except Exception:
                    pass
            threading.Thread(target=close_device_handle, args=(device,), daemon=True,
                             name="BloodyCloseDevice").start()
        try:
            if self._worker_poll_id is not None:
                self.root.after_cancel(self._worker_poll_id)
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

def main():
    root = tk.Tk()
    app = BloodyControl(root)
    root.mainloop()

if __name__ == "__main__":
    main()
