"""Declarative forms: groups of fields mapped onto preset sections."""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QGroupBox, QScrollArea, QVBoxLayout, QWidget

from core.config import (
    HW_ACCEL_CHOICES,
    SUB_POSITIONS,
    WHISPER_MODELS,
    X264_PRESETS,
    ConfigError,
    Preset,
)
from gui.fields import (
    CheckField,
    ChoiceField,
    ColorField,
    Field,
    FloatField,
    IntField,
    ListField,
    PathField,
    RangeField,
    TextField,
)

Spec = tuple[str, str, Field]  # (dataclass attribute, label, field)

AUDIO_FILTER = "Аудио (*.mp3 *.m4a *.aac *.wav *.ogg *.flac);;Все файлы (*)"
FFMPEG_FILTER = "ffmpeg (ffmpeg ffmpeg.exe);;Все файлы (*)"
MAX_WORKERS = 64


class FieldGroup:
    """Titled set of fields bound to one dataclass (a preset section or an AdSlot)."""

    def __init__(self, title: str, section: str, specs: list[Spec]):
        self.title = title
        self.section = section
        self.specs = specs
        self.fields = {attr: field for attr, _, field in specs}

    def build(self) -> QGroupBox:
        box = QGroupBox(self.title)
        layout = QFormLayout(box)
        for _, label, field in self.specs:
            layout.addRow(label, field.widget)
        return box

    def load(self, obj) -> None:
        for attr, field in self.fields.items():
            field.set(getattr(obj, attr))

    def values(self) -> dict:
        try:
            return {attr: field.get() for attr, field in self.fields.items()}
        except ConfigError as exc:
            raise ConfigError(f"{self.title}: {exc}") from exc

    def connect(self, callback) -> None:
        for field in self.fields.values():
            field.connect(callback)


class SectionForm(QScrollArea):
    """Scrollable stack of field groups; ``apply`` returns an updated preset copy."""

    changed = Signal()

    def __init__(self, groups: list[FieldGroup], parent: QWidget | None = None):
        super().__init__(parent)
        self.groups = groups
        inner = QWidget()
        layout = QVBoxLayout(inner)
        for group in groups:
            layout.addWidget(group.build())
            group.connect(self.changed.emit)
        layout.addStretch(1)
        self.setWidget(inner)
        self.setWidgetResizable(True)

    def field(self, section: str, attr: str) -> Field:
        for group in self.groups:
            if group.section == section and attr in group.fields:
                return group.fields[attr]
        raise KeyError(f"{section}.{attr}")

    def load(self, obj) -> None:
        """Fill from a preset, or from the object itself for section-less groups."""
        for group in self.groups:
            group.load(getattr(obj, group.section) if group.section else obj)

    def apply(self, preset: Preset) -> Preset:
        sections: dict = {}
        for group in self.groups:
            current = sections.get(group.section, getattr(preset, group.section))
            values = group.values()
            try:
                sections[group.section] = replace(current, **values)
            except ConfigError as exc:
                raise ConfigError(f"{group.title}: {exc}") from exc
        return replace(preset, **sections)


# ------------------------------------------------------------------ specs


def _percent(lo: float = 0.0, hi: float = 20.0) -> RangeField:
    return RangeField(lo, hi, 0.5, 1, " %")


def _db(lo: float = -10.0, hi: float = 10.0) -> RangeField:
    return RangeField(lo, hi, 0.5, 1, " дБ")


def _factor() -> RangeField:
    return RangeField(0.5, 1.5, 0.01, 2)


def _seconds(hi: float = 10.0) -> FloatField:
    return FloatField(0.0, hi, 0.1, 1, " с")


def _even_size() -> IntField:
    field = IntField(64, 4096)
    field.widget.setSingleStep(2)
    return field


def uniq_groups() -> list[FieldGroup]:
    return [
        FieldGroup("Разрешение и кадрирование", "uniq", [
            ("resolution_enabled", "Включено", CheckField()),
            ("target_width", "Ширина", _even_size()),
            ("target_height", "Высота", _even_size()),
            ("scale_pct", "Масштаб", _percent()),
            ("zoom_pct", "Кроп-зум", _percent()),
        ]),
        FieldGroup("Цвет", "uniq", [
            ("color_enabled", "Включено", CheckField()),
            ("brightness", "Яркость", RangeField(-0.2, 0.2, 0.005, 3)),
            ("contrast", "Контраст", _factor()),
            ("saturation", "Насыщенность", _factor()),
            ("gamma", "Гамма", _factor()),
            ("hue_deg", "Оттенок", RangeField(-30.0, 30.0, 0.5, 1, "°")),
            ("noise_enabled", "Шум", CheckField()),
            ("noise_strength", "Сила шума", RangeField(0.0, 30.0, 0.5, 1)),
            ("vignette_enabled", "Виньетка", CheckField()),
            ("vignette_angle", "Угол виньетки", RangeField(0.0, 1.2, 0.01, 2)),
        ]),
        FieldGroup("Звук", "uniq", [
            ("audio_enabled", "Громкость", CheckField()),
            ("volume_db", "Изменение громкости", _db()),
            ("eq_enabled", "Эквалайзер", CheckField()),
            ("eq_gain_db", "Усиление полосы", _db()),
            ("pitch_enabled", "Тон", CheckField()),
            ("pitch_pct", "Сдвиг тона", RangeField(-10.0, 10.0, 0.1, 1, " %")),
        ]),
        FieldGroup("Время", "uniq", [
            ("time_enabled", "Включено", CheckField()),
            ("speed", "Скорость", RangeField(0.8, 1.2, 0.005, 3, "×")),
            ("trim_start", "Обрезка начала", RangeField(0.0, 3.0, 0.05, 2, " с")),
            ("trim_end", "Обрезка конца", RangeField(0.0, 3.0, 0.05, 2, " с")),
            ("fps_enabled", "Смена FPS", CheckField()),
            ("fps_choices", "Варианты FPS", ListField(float, "30; 29.97; 60")),
        ]),
        FieldGroup("Отражение", "uniq", [
            ("flip_enabled", "Зеркалить по горизонтали", CheckField()),
            ("flip_probability", "Вероятность", FloatField(0.0, 1.0, 0.05, 2)),
        ]),
        FieldGroup("Кодирование", "encoder", [
            ("crf", "CRF", RangeField(0.0, 51.0, 1.0, 0)),
            ("preset", "Пресет x264", ChoiceField(X264_PRESETS)),
            ("audio_bitrate_k", "Битрейт AAC", RangeField(32.0, 512.0, 8.0, 0, " кбит/с")),
            ("hwaccel", "Аппаратный энкодер", ChoiceField(HW_ACCEL_CHOICES)),
            ("strip_encoder_sei", "Убирать подпись x264", CheckField()),
        ]),
    ]


def metadata_groups() -> list[FieldGroup]:
    return [
        FieldGroup("Метаданные", "metadata", [
            ("date_from", "Дата съёмки от", TextField("ГГГГ-ММ-ДД, пусто = год назад")),
            ("date_to", "Дата съёмки до", TextField("ГГГГ-ММ-ДД, пусто = сегодня")),
            ("title_mode", "Название", ChoiceField([("empty", "Пустое"),
                                                    ("neutral", "Нейтральное из списка")])),
            ("neutral_titles", "Нейтральные названия", ListField(str, "video; clip; IMG")),
            ("verify", "Проверять через ffprobe", CheckField()),
        ]),
    ]


def music_groups() -> list[FieldGroup]:
    return [
        FieldGroup("Фоновая музыка", "music", [
            ("enabled", "Включено", CheckField()),
            ("path", "Трек или папка", PathField(("file", "dir"), AUDIO_FILTER)),
            ("volume_db", "Громкость музыки", FloatField(-40.0, 10.0, 0.5, 1, " дБ")),
            ("original_volume_db", "Громкость оригинала", FloatField(-40.0, 10.0, 0.5, 1, " дБ")),
            ("replace_audio", "Заменить оригинальный звук", CheckField()),
            ("random_start", "Случайное место в треке", CheckField()),
            ("fade_in", "Нарастание", _seconds()),
            ("fade_out", "Затухание", _seconds()),
        ]),
        FieldGroup("Приглушение под голосом", "music", [
            ("ducking", "Включено", CheckField()),
            ("duck_ratio", "Степень", FloatField(1.0, 20.0, 0.5, 1)),
            ("duck_threshold", "Порог", FloatField(0.001, 1.0, 0.005, 3)),
        ]),
    ]


def subtitle_groups() -> list[FieldGroup]:
    return [
        FieldGroup("Источник", "subtitles", [
            ("enabled", "Включено", CheckField()),
            ("source", "Откуда брать", ChoiceField([("file", "Файл .srt/.ass рядом с видео"),
                                                     ("auto", "Распознать речь (faster-whisper)")])),
            ("whisper_model", "Модель whisper", ChoiceField(WHISPER_MODELS)),
            ("language", "Язык", TextField("ru, en, … пусто = автоопределение")),
            ("save_srt", "Сохранять .srt рядом с результатом", CheckField()),
        ]),
        FieldGroup("Стиль", "subtitles", [
            ("font", "Шрифт", TextField()),
            ("fonts_dir", "Папка шрифтов", PathField(("dir",))),
            ("font_size", "Размер", IntField(8, 300)),
            ("bold", "Жирный", CheckField()),
            ("uppercase", "ЗАГЛАВНЫЕ", CheckField()),
            ("primary_color", "Цвет текста", ColorField()),
            ("highlight_color", "Цвет подсветки", ColorField()),
            ("outline_color", "Цвет обводки", ColorField()),
            ("outline", "Обводка", FloatField(0.0, 20.0, 0.5, 1)),
            ("shadow", "Тень", FloatField(0.0, 20.0, 0.5, 1)),
            ("position", "Позиция", ChoiceField([(p, label) for p, label in
                                                  zip(SUB_POSITIONS, ("Снизу", "По центру", "Сверху"))])),
            ("margin_v", "Отступ по вертикали", IntField(0, 1900)),
        ]),
        FieldGroup("Стиль Shorts", "subtitles", [
            ("shorts_style", "По несколько слов", CheckField()),
            ("max_words", "Слов на экране", IntField(1, 10)),
            ("highlight_current_word", "Подсвечивать текущее слово", CheckField()),
        ]),
    ]


def output_groups() -> list[FieldGroup]:
    return [
        FieldGroup("Результат", "output", [
            ("recursive", "Искать во вложенных папках", CheckField()),
            ("output_dir", "Папка результата", PathField(("dir",))),
            ("name_template", "Шаблон имени", TextField("IMG_{random4}_{date}")),
            ("variants", "Вариантов на видео", IntField(1, 100)),
        ]),
    ]


def batch_groups() -> list[FieldGroup]:
    workers = IntField(0, MAX_WORKERS)
    workers.widget.setSpecialValueText("авто")
    workers.widget.setMinimumWidth(workers.widget.fontMetrics().horizontalAdvance("авто") * 2)
    return [
        FieldGroup("Очередь", "batch", [
            ("workers", "Параллельных задач", workers),
            ("seed", "Сид", TextField("пусто = случайно")),
            ("skip_existing", "Пропускать готовые", CheckField()),
            ("preview_seconds", "Длина превью", FloatField(1.0, 120.0, 1.0, 0, " с")),
            ("ffmpeg_path", "Путь к ffmpeg", PathField(("file", "dir"), FFMPEG_FILTER,
                                                       "пусто = из PATH")),
        ]),
    ]
