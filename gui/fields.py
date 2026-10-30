"""Form fields bound to preset values, plus a two-handle min–max slider.

Every field exposes ``widget``, ``get()``, ``set(value)`` and ``connect(callback)``,
so forms can be declared as data and round-trip a frozen settings dataclass.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QWidget,
)

from core.config import ConfigError, Range


def _row(*widgets: QWidget) -> QWidget:
    box = QWidget()
    layout = QHBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    for w in widgets:
        layout.addWidget(w)
    return box


class Field:
    widget: QWidget

    def get(self):
        raise NotImplementedError

    def set(self, value) -> None:
        raise NotImplementedError

    def connect(self, callback: Callable[[], None]) -> None:
        raise NotImplementedError


class CheckField(Field):
    def __init__(self, text: str = ""):
        self.widget = QCheckBox(text)

    def get(self) -> bool:
        return self.widget.isChecked()

    def set(self, value: bool) -> None:
        self.widget.setChecked(bool(value))

    def connect(self, callback):
        self.widget.toggled.connect(lambda _: callback())


class IntField(Field):
    def __init__(self, lo: int, hi: int, suffix: str = ""):
        self.widget = QSpinBox()
        self.widget.setRange(lo, hi)
        self.widget.setSuffix(suffix)

    def get(self) -> int:
        return self.widget.value()

    def set(self, value: int) -> None:
        self.widget.setValue(int(value))

    def connect(self, callback):
        self.widget.valueChanged.connect(lambda _: callback())


def _double_spin(lo: float, hi: float, step: float, decimals: int, suffix: str) -> QDoubleSpinBox:
    spin = QDoubleSpinBox()
    spin.setDecimals(decimals)
    spin.setRange(lo, hi)
    spin.setSingleStep(step)
    spin.setSuffix(suffix)
    return spin


class FloatField(Field):
    def __init__(self, lo: float, hi: float, step: float = 0.1, decimals: int = 2, suffix: str = ""):
        self.widget = _double_spin(lo, hi, step, decimals, suffix)

    def get(self) -> float:
        return round(self.widget.value(), self.widget.decimals())

    def set(self, value: float) -> None:
        self.widget.setValue(float(value))

    def connect(self, callback):
        self.widget.valueChanged.connect(lambda _: callback())


class TextField(Field):
    def __init__(self, placeholder: str = ""):
        self.widget = QLineEdit()
        self.widget.setPlaceholderText(placeholder)

    def get(self) -> str:
        return self.widget.text().strip()

    def set(self, value: str) -> None:
        self.widget.setText(str(value))

    def connect(self, callback):
        self.widget.textChanged.connect(lambda _: callback())


class ChoiceField(Field):
    def __init__(self, choices):
        """``choices``: values, or (value, label) pairs."""
        self.widget = QComboBox()
        for choice in choices:
            value, label = choice if isinstance(choice, tuple) else (choice, str(choice))
            self.widget.addItem(label, value)

    def get(self):
        return self.widget.currentData()

    def set(self, value) -> None:
        index = self.widget.findData(value)
        if index < 0:
            raise ConfigError(f"Недопустимое значение: {value!r}")
        self.widget.setCurrentIndex(index)

    def connect(self, callback):
        self.widget.currentIndexChanged.connect(lambda _: callback())


class ListField(TextField):
    """A tuple edited as ``a; b; c`` (``;`` because titles contain spaces, floats commas)."""

    def __init__(self, cast: Callable[[str], object] = str, placeholder: str = ""):
        super().__init__(placeholder)
        self.cast = cast

    def get(self) -> tuple:
        items = [part.strip() for part in self.widget.text().split(";") if part.strip()]
        try:
            return tuple(self.cast(item.replace(",", ".") if self.cast is float else item)
                         for item in items)
        except ValueError as exc:
            raise ConfigError(f"Неверный список: {self.widget.text()}") from exc

    def set(self, value) -> None:
        self.widget.setText("; ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in value))


class PathField(TextField):
    """Path edit with browse buttons; ``kinds`` is a subset of {"file", "dir"}."""

    def __init__(self, kinds=("file",), file_filter: str = "", placeholder: str = ""):
        super().__init__(placeholder)
        self.edit = self.widget
        self.file_filter = file_filter
        buttons = []
        if "file" in kinds:
            buttons.append(self._button("Файл…", self._pick_file))
        if "dir" in kinds:
            buttons.append(self._button("Папка…", self._pick_dir))
        self.widget = _row(self.edit, *buttons)

    def _button(self, text: str, slot) -> QPushButton:
        button = QPushButton(text)
        button.clicked.connect(slot)
        return button

    def _pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self.widget, "Выберите файл", self.edit.text(),
                                              self.file_filter)
        if path:
            self.edit.setText(path)

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self.widget, "Выберите папку", self.edit.text())
        if path:
            self.edit.setText(path)

    def get(self) -> str:
        return self.edit.text().strip()

    def set(self, value: str) -> None:
        self.edit.setText(str(value))

    def connect(self, callback):
        self.edit.textChanged.connect(lambda _: callback())


class ColorField(PathField):
    """``#RRGGBB`` with an optional ``@alpha`` suffix kept when picking a colour."""

    def __init__(self, allow_empty: bool = False):
        TextField.__init__(self, "пусто = выкл." if allow_empty else "#RRGGBB")
        self.edit = self.widget
        self.widget = _row(self.edit, self._button("Цвет…", self._pick_color))

    def _pick_color(self) -> None:
        hex_part, sep, alpha = self.edit.text().partition("@")
        color = QColorDialog.getColor(QColor(hex_part or "#FFFFFF"), self.widget, "Цвет")
        if color.isValid():
            self.edit.setText(color.name().upper() + (sep + alpha if sep else ""))


# ------------------------------------------------------------- range slider


class RangeSlider(QWidget):
    """Horizontal slider with a low and a high handle."""

    rangeChanged = Signal(float, float)
    HANDLE_RADIUS = 7
    GROOVE_HEIGHT = 4

    def __init__(self, lo: float, hi: float, parent: QWidget | None = None):
        super().__init__(parent)
        self.lo, self.hi = lo, hi
        self._a, self._b = lo, hi
        self._drag: int | None = None  # 0 = low handle, 1 = high handle
        self.setMinimumSize(120, 2 * self.HANDLE_RADIUS + 6)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def values(self) -> tuple[float, float]:
        return self._a, self._b

    def setBounds(self, lo: float, hi: float) -> None:
        self.lo, self.hi = lo, hi
        self.update()

    def setValues(self, a: float, b: float) -> None:
        a, b = sorted((min(max(a, self.lo), self.hi), min(max(b, self.lo), self.hi)))
        if (a, b) != (self._a, self._b):
            self._a, self._b = a, b
            self.update()
            self.rangeChanged.emit(a, b)

    def _span(self) -> float:
        return max(self.width() - 2 * self.HANDLE_RADIUS, 1)

    def _to_x(self, value: float) -> float:
        frac = 0.0 if self.hi == self.lo else (value - self.lo) / (self.hi - self.lo)
        return self.HANDLE_RADIUS + frac * self._span()

    def _to_value(self, x: float) -> float:
        frac = min(max((x - self.HANDLE_RADIUS) / self._span(), 0.0), 1.0)
        return self.lo + frac * (self.hi - self.lo)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        mid_y = self.height() / 2
        groove = QRectF(self.HANDLE_RADIUS, mid_y - self.GROOVE_HEIGHT / 2,
                        self._span(), self.GROOVE_HEIGHT)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(pal.color(QPalette.ColorRole.Mid))
        painter.drawRoundedRect(groove, 2, 2)
        xa, xb = self._to_x(self._a), self._to_x(self._b)
        painter.setBrush(pal.color(QPalette.ColorRole.Highlight))
        painter.drawRect(QRectF(xa, groove.top(), xb - xa, groove.height()))
        painter.setPen(pal.color(QPalette.ColorRole.Dark))
        painter.setBrush(pal.color(QPalette.ColorRole.Button))
        for x in (xa, xb):
            painter.drawEllipse(QPointF(x, mid_y), self.HANDLE_RADIUS, self.HANDLE_RADIUS)

    def mousePressEvent(self, event) -> None:
        x = event.position().x()
        xa, xb = self._to_x(self._a), self._to_x(self._b)
        if xa == xb:  # handles stacked: pick by the side that was clicked
            self._drag = 0 if x < xa else 1
        else:
            self._drag = 0 if abs(x - xa) < abs(x - xb) else 1
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag is None:
            return
        value = self._to_value(event.position().x())
        if self._drag == 0:
            self.setValues(min(value, self._b), self._b)
        else:
            self.setValues(self._a, max(value, self._a))

    def mouseReleaseEvent(self, _event) -> None:
        self._drag = None


class RangeField(Field):
    """Min spinbox + two-handle slider + max spinbox for a :class:`Range`."""

    def __init__(self, lo: float, hi: float, step: float = 0.1, decimals: int = 2, suffix: str = ""):
        self.decimals = decimals
        self.slider = RangeSlider(lo, hi)
        self.min_spin = _double_spin(lo, hi, step, decimals, suffix)
        self.max_spin = _double_spin(lo, hi, step, decimals, suffix)
        for spin in (self.min_spin, self.max_spin):
            spin.setMinimumWidth(80)
        self.widget = _row(self.min_spin, self.slider, self.max_spin)
        self._syncing = False
        self.slider.rangeChanged.connect(self._from_slider)
        self.min_spin.valueChanged.connect(lambda v: self._from_spin(v, self.max_spin.value(), 0))
        self.max_spin.valueChanged.connect(lambda v: self._from_spin(self.min_spin.value(), v, 1))

    def _from_slider(self, a: float, b: float) -> None:
        if self._syncing:
            return
        self._syncing = True
        self.min_spin.setValue(a)
        self.max_spin.setValue(b)
        self._syncing = False

    def _from_spin(self, a: float, b: float, edited: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        if a > b:  # keep min <= max by dragging the other end along
            if edited == 0:
                self.max_spin.setValue(a)
                b = a
            else:
                self.min_spin.setValue(b)
                a = b
        self.slider.setValues(a, b)
        self._syncing = False

    def get(self) -> Range:
        return Range(round(self.min_spin.value(), self.decimals),
                     round(self.max_spin.value(), self.decimals))

    def set(self, value: Range) -> None:
        lo = min(self.slider.lo, value.min)  # a preset may go beyond the usual bounds
        hi = max(self.slider.hi, value.max)
        self._syncing = True
        for spin in (self.min_spin, self.max_spin):
            spin.setRange(lo, hi)
        self.slider.setBounds(lo, hi)
        self.min_spin.setValue(value.min)
        self.max_spin.setValue(value.max)
        self.slider.setValues(value.min, value.max)
        self._syncing = False

    def connect(self, callback):
        self.min_spin.valueChanged.connect(lambda _: callback())
        self.max_spin.valueChanged.connect(lambda _: callback())
