"""A complete, predictable light client theme for the desktop prototype.

System dark palettes must not supply white foregrounds to our light scientific
workspace. No operating-system setting or scientific display value is changed.
"""
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


DESKTOP_STYLE = """
QWidget { color:#29444c; }
QMainWindow,QDialog { background:#eef3f4; }
QLabel,QCheckBox,QRadioButton { color:#29444c; }
QGroupBox { color:#29444c; background:white; border:1px solid #ccdade; border-radius:6px; margin-top:8px; padding-top:13px; font-weight:600; }
QGroupBox::title { color:#29444c; subcontrol-origin:margin; left:10px; padding:0 4px; }
QPushButton { padding:6px 10px; border:1px solid #b4cbd0; border-radius:4px; background:#fff; color:#24535f; }
QPushButton:hover { background:#e4f0f1; color:#24535f; }
QPushButton:disabled { color:#64767c; background:#edf1f2; }
QPushButton[primary=true] { background:#276c77; color:white; }
QPushButton[primary=true]:disabled { background:#d5e0e2; color:#64767c; }
QLineEdit,QPlainTextEdit,QTextEdit,QComboBox,QAbstractSpinBox { padding:5px; background:white; color:#29444c; selection-background-color:#276c77; selection-color:white; border:1px solid #bccfd4; border-radius:3px; }
QLineEdit:disabled,QPlainTextEdit:disabled,QTextEdit:disabled,QComboBox:disabled,QAbstractSpinBox:disabled { color:#64767c; background:#edf1f2; }
QTreeWidget,QTreeView,QListWidget,QListView,QTableWidget,QTableView { color:#29444c; border:1px solid #cfdbde; background:white; alternate-background-color:#f4f8f8; }
QTreeWidget::item:selected,QTreeView::item:selected,QListWidget::item:selected,QListView::item:selected,QTableView::item:selected { background:#d4e8eb; color:#174d58; }
QHeaderView::section { background:#e8f0f1; color:#29444c; padding:4px; border:1px solid #cfdbde; }
QComboBox QAbstractItemView { background:white; color:#29444c; selection-background-color:#276c77; selection-color:white; }
QMenuBar,QMenu { background:#f4f8f8; color:#29444c; }
QMenuBar::item:selected,QMenu::item:selected { background:#d4e8eb; color:#174d58; }
QToolTip { background:#f7fafa; color:#29444c; border:1px solid #bccfd4; }
QFrame#itemEditor { background:#fff; border:1px solid #ccdade; border-radius:4px; }
QScrollArea#itemFormScroll, QWidget#itemExtraFields { background:#fff; border:0; }
QFrame#pageSaveBar { background:#f4f8f8; border-top:1px solid #bccfd4; }
QLabel#itemStatus { color:#506970; }
QPushButton#previousItem,QPushButton#nextItem,QToolButton#notesToggle {
    background:transparent; color:#315d68; border:0; border-radius:3px; padding:3px 6px;
}
QPushButton#previousItem:hover,QPushButton#nextItem:hover,QToolButton#notesToggle:hover { background:#e4f0f1; }
QToolButton#itemTools,QToolButton#itemSaveOptions {
    background:#fff; color:#24535f; border:1px solid #b4cbd0; border-radius:4px; padding:4px 6px;
}
QToolButton#itemTools:hover,QToolButton#itemSaveOptions:hover { background:#e4f0f1; }
QToolButton#itemSaveOptions::menu-indicator { image:none; }
QPushButton#previousItem:focus,QPushButton#nextItem:focus,QToolButton#notesToggle:focus,
QToolButton#itemTools:focus,QToolButton#itemSaveOptions:focus { outline:1px solid #276c77; }
QStatusBar { background:#eef3f4; color:#315d68; }
QSplitter::handle { background:#d3e0e3; }
QScrollArea { background:white; }
QCheckBox:disabled,QRadioButton:disabled,QLabel:disabled { color:#64767c; }
"""


def apply_light_theme(application=None):
    """Apply the same explicit foreground/background policy to every dialog."""
    application = application or QApplication.instance()
    if application is None:
        raise RuntimeError("Create QApplication before applying the desktop theme.")
    # Fusion honours the application palette consistently across system themes.
    if application.style().objectName().lower() != "fusion":
        application.setStyle("Fusion")
    palette = QPalette()
    colors = {
        QPalette.ColorRole.Window: "#eef3f4",
        QPalette.ColorRole.WindowText: "#29444c",
        QPalette.ColorRole.Base: "#ffffff",
        QPalette.ColorRole.AlternateBase: "#f4f8f8",
        QPalette.ColorRole.Text: "#29444c",
        QPalette.ColorRole.Button: "#ffffff",
        QPalette.ColorRole.ButtonText: "#24535f",
        QPalette.ColorRole.Highlight: "#276c77",
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.ToolTipBase: "#f7fafa",
        QPalette.ColorRole.ToolTipText: "#29444c",
        QPalette.ColorRole.PlaceholderText: "#61767e",
        QPalette.ColorRole.Link: "#1b697b",
        QPalette.ColorRole.LinkVisited: "#536a83",
    }
    for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
                  QPalette.ColorGroup.Disabled):
        for role, color in colors.items():
            palette.setColor(group, role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText,
                 QPalette.ColorRole.ButtonText, QPalette.ColorRole.PlaceholderText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#64767c"))
    for role in (QPalette.ColorRole.Base, QPalette.ColorRole.Button):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#edf1f2"))
    application.setPalette(palette)
    application.setStyleSheet(DESKTOP_STYLE)
    return palette
