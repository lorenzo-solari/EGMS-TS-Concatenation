import os

# ---------------------------------------------------------------------------
# Qt compatibility: QGIS 3 (PyQt5/Qt5) and QGIS 4 (PyQt6/Qt6)
# ---------------------------------------------------------------------------
try:
    from qgis.PyQt.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
        QPushButton, QLineEdit, QFileDialog, QCheckBox, QComboBox,
        QGroupBox, QSizePolicy, QSpacerItem, QMessageBox, QProgressBar
    )
    from qgis.PyQt.QtCore import Qt, QObject, pyqtSignal
    from qgis.PyQt.QtGui import QFont
except ImportError:
    from PyQt6.QtWidgets import (
        QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
        QPushButton, QLineEdit, QFileDialog, QCheckBox, QComboBox,
        QGroupBox, QSizePolicy, QSpacerItem, QMessageBox, QProgressBar
    )
    from PyQt6.QtCore import Qt, QObject, pyqtSignal
    from PyQt6.QtGui import QFont

_SP = getattr(QSizePolicy, 'Policy', QSizePolicy)

from qgis.core import QgsProject, QgsVectorLayer

# QgsMapLayerType removed in QGIS 4
try:
    from qgis.core import Qgis
    VECTOR_LAYER_TYPE = Qgis.LayerType.Vector
except AttributeError:
    try:
        from qgis.core import QgsMapLayerType
        VECTOR_LAYER_TYPE = QgsMapLayerType.VectorLayer
    except ImportError:
        VECTOR_LAYER_TYPE = None

from .egms_processing import run_concat, crs, x_field, y_field


def _bold(widget):
    f = widget.font()
    try:
        f.setBold(True)
    except Exception:
        f.setWeight(QFont.Weight.Bold)
    widget.setFont(f)


# ---------------------------------------------------------------------------
# Signal bridge: lets the background thread safely talk to the Qt main thread
# Qt signals are always delivered on the thread that owns the receiver object,
# so emitting from a worker thread safely queues the call onto the GUI thread.
# ---------------------------------------------------------------------------
class _WorkerSignals(QObject):
    progress  = pyqtSignal(int, str)   # (percent, message)
    finished  = pyqtSignal(str)        # output_csv path
    error     = pyqtSignal(str)        # error message


# ===========================================================================
# Reusable input widget
# ===========================================================================
class LayerOrFileWidget(QGroupBox):
    def __init__(self, title, file_filter="CSV files (*.csv)", parent=None):
        super().__init__(title, parent)
        self.file_filter = file_filter

        outer = QVBoxLayout(self)
        outer.setSpacing(5)

        lbl_layers = QLabel("Layer loaded in QGIS:")
        _bold(lbl_layers)
        outer.addWidget(lbl_layers)

        self.combo = QComboBox()
        self.combo.setMinimumHeight(28)
        self.combo.setSizePolicy(_SP.Expanding, _SP.Fixed)
        outer.addWidget(self.combo)

        lbl_file = QLabel("Or browse for a file:")
        _bold(lbl_file)
        outer.addWidget(lbl_file)

        browse_row = QHBoxLayout()
        self.line = QLineEdit()
        self.line.setPlaceholderText("Path to file ...")
        self.line.setMinimumHeight(28)
        browse_btn = QPushButton("Browse...")
        browse_btn.setFixedWidth(90)
        browse_btn.setMinimumHeight(28)
        browse_row.addWidget(self.line)
        browse_row.addWidget(browse_btn)
        outer.addLayout(browse_row)

        browse_btn.clicked.connect(self._browse)
        self.combo.currentIndexChanged.connect(self._on_combo_changed)
        self.line.textEdited.connect(self._on_line_edited)

    def populate(self, layer_type=None):
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("-- select a loaded layer --", userData=None)
        for layer in QgsProject.instance().mapLayers().values():
            if layer_type is not None and layer.type() != layer_type:
                continue
            self.combo.addItem(layer.name(), userData=layer.source())
        self.combo.blockSignals(False)

    def path(self):
        return self.line.text().strip()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select File", "", self.file_filter)
        if path:
            self.line.setText(path)
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(0)
            self.combo.blockSignals(False)

    def _on_combo_changed(self, _index):
        source = self.combo.currentData()
        if source:
            clean = source.split("?")[0]
            if clean.startswith("file:///"):
                clean = clean[8:]
            self.line.setText(clean)

    def _on_line_edited(self, _text):
        self.combo.blockSignals(True)
        self.combo.setCurrentIndex(0)
        self.combo.blockSignals(False)


# ===========================================================================
# MAIN DIALOG
# ===========================================================================
class EGMSDialog(QDialog):

    def __init__(self, iface):
        super().__init__()
        self.iface   = iface
        self._thread = None

        # Signal bridge – lives on the main thread, so all slots run here
        self._signals = _WorkerSignals()
        self._signals.progress.connect(self._on_progress)
        self._signals.finished.connect(self._on_finished)
        self._signals.error.connect(self._on_error)

        self.setWindowTitle("EGMS Time Series Concatenation")
        self.setMinimumSize(700, 620)
        self.resize(740, 680)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(14)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # -- Period 1 -----------------------------------------------------
        self.w_csv1 = LayerOrFileWidget("Period 1 CSV",
                                        file_filter="CSV files (*.csv)")
        main_layout.addWidget(self.w_csv1)

        # -- Period 2 -----------------------------------------------------
        self.w_csv2 = LayerOrFileWidget("Period 2 CSV",
                                        file_filter="CSV files (*.csv)")
        main_layout.addWidget(self.w_csv2)

        # -- AOI (optional) -----------------------------------------------
        self.use_aoi = QCheckBox("Clip to AOI (optional)")
        _bold(self.use_aoi)
        main_layout.addWidget(self.use_aoi)

        self.w_aoi = LayerOrFileWidget("AOI Shapefile",
                                       file_filter="Shapefile (*.shp)")
        self.w_aoi.setEnabled(False)
        main_layout.addWidget(self.w_aoi)
        self.use_aoi.toggled.connect(self.w_aoi.setEnabled)

        # -- Output -------------------------------------------------------
        out_group = QGroupBox("Output")
        out_grid  = QGridLayout(out_group)
        out_grid.setSpacing(8)
        out_grid.addWidget(QLabel("Output CSV file:"), 0, 0)
        self.out_csv = QLineEdit()
        self.out_csv.setPlaceholderText("Path to output .csv ...")
        self.out_csv.setMinimumHeight(28)
        out_grid.addWidget(self.out_csv, 0, 1)
        btn_out = QPushButton("Browse...")
        btn_out.setFixedWidth(90)
        btn_out.setMinimumHeight(28)
        btn_out.clicked.connect(self._select_output)
        out_grid.addWidget(btn_out, 0, 2)
        main_layout.addWidget(out_group)

        # -- Progress bar -------------------------------------------------
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setVisible(False)
        main_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("")
        self.status_label.setVisible(False)
        main_layout.addWidget(self.status_label)

        # -- Spacer -------------------------------------------------------
        main_layout.addSpacerItem(
            QSpacerItem(20, 10, _SP.Minimum, _SP.Expanding))

        # -- Button bar ---------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.addSpacerItem(
            QSpacerItem(40, 20, _SP.Expanding, _SP.Minimum))

        self.run_button = QPushButton("Run")
        self.run_button.setMinimumHeight(36)
        self.run_button.setMinimumWidth(120)
        self.run_button.setDefault(True)
        _bold(self.run_button)
        self.run_button.clicked.connect(self.execute)
        btn_row.addWidget(self.run_button)

        self.close_button = QPushButton("Close")
        self.close_button.setMinimumHeight(36)
        self.close_button.setMinimumWidth(90)
        self.close_button.clicked.connect(self.reject)
        btn_row.addWidget(self.close_button)

        main_layout.addLayout(btn_row)

        self._refresh_layers()

    # ------------------------------------------------------------------
    def _refresh_layers(self):
        for w in (self.w_csv1, self.w_csv2, self.w_aoi):
            w.populate(layer_type=VECTOR_LAYER_TYPE)

    def _select_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Select Output CSV", "", "CSV files (*.csv)")
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            self.out_csv.setText(path)

    # ------------------------------------------------------------------
    def execute(self):
        csv1   = self.w_csv1.path()
        csv2   = self.w_csv2.path()
        output = self.out_csv.text().strip()

        if not csv1 or not csv2 or not output:
            QMessageBox.warning(
                self, "Missing inputs",
                "Please provide Period 1 CSV, Period 2 CSV, and an output file path.")
            return

        layer_name = os.path.splitext(os.path.basename(output))[0]
        aoi_path   = self.w_aoi.path() if self.use_aoi.isChecked() else None

        self._layer_name = layer_name

        # Show progress UI, disable buttons
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.status_label.setText("Starting...")
        self.status_label.setVisible(True)
        self.run_button.setEnabled(False)
        self.close_button.setEnabled(False)

        # Callbacks emit Qt signals → safely received on the main thread
        sig = self._signals
        self._thread = run_concat(
            csv1, csv2, output,
            clip_shapefile=aoi_path,
            iface=self.iface,
            layer_name=layer_name,
            on_progress=lambda pct, msg: sig.progress.emit(pct, msg),
            on_done=lambda path: sig.finished.emit(path),
            on_error=lambda exc: sig.error.emit(str(exc)),
        )

    # ------------------------------------------------------------------
    # Slots – always called on the main thread via Qt signal delivery
    # ------------------------------------------------------------------
    def _on_progress(self, pct, msg):
        self.progress_bar.setValue(pct)
        self.status_label.setText(msg)

    def _on_finished(self, output_csv):
        # Build a robust URI that works on both QGIS 3 and QGIS 4.
        # detectTypes=yes is required in QGIS 4 to prevent all fields
        # being read as strings (which causes numeric attributes to appear empty).
        # Path.as_uri() produces the correct file:/// prefix on all platforms.
        from pathlib import Path
        file_url = Path(output_csv).as_uri()
        uri = (
            f"{file_url}"
            f"?delimiter=,"
            f"&xField={x_field}"
            f"&yField={y_field}"
            f"&crs={crs}"
            f"&detectTypes=yes"
            f"&geomType=point"
            f"&trimFields=yes"
            f"&skipEmptyFields=no"
        )
        layer = QgsVectorLayer(uri, self._layer_name, "delimitedtext")
        if layer.isValid():
            QgsProject.instance().addMapLayer(layer)

        self._reset_ui()
        QMessageBox.information(
            self, "Done",
            f"Processing complete.\n"
            f"Layer '{self._layer_name}' has been loaded in QGIS.")
        self.accept()

    def _on_error(self, msg):
        self._reset_ui()
        QMessageBox.critical(self, "Error", msg)

    def _reset_ui(self):
        self.run_button.setEnabled(True)
        self.close_button.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.status_label.setVisible(False)
