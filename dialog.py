from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QPushButton,
    QLineEdit,
    QFileDialog,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QSizePolicy,
    QSpacerItem,
    QMessageBox
)
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QFont

from qgis.core import (
    QgsProject,
    QgsMapLayerType,
    QgsTask,
    QgsApplication,
    QgsVectorLayer,
    QgsMessageLog,
    Qgis
)

from .egms_processing import run_concat

LOG_TAG = "EGMS Concatenation"


# ======================================================================
# BACKGROUND TASK
# ======================================================================

class EGMSConcatTask(QgsTask):
    """Runs run_concat in a background thread via the QGIS task manager."""

    def __init__(self, csv1, csv2, output, aoi_path, layer_name, iface):
        super().__init__("EGMS TS Concatenation", QgsTask.CanCancel)
        self.csv1       = csv1
        self.csv2       = csv2
        self.output     = output
        self.aoi_path   = aoi_path
        self.layer_name = layer_name
        self.iface      = iface
        self.error      = None

    # ------------------------------------------------------------------

    def run(self):
        try:
            # Pass iface=None so run_concat does NOT try to load the layer
            # inside the worker thread (Qt widgets must be created on the
            # main thread). We load the layer in finished() instead.
            run_concat(
                self.csv1,
                self.csv2,
                self.output,
                clip_shapefile=self.aoi_path,
                iface=None,
                layer_name=self.layer_name
            )
            return True
        except Exception as e:
            self.error = str(e)
            QgsMessageLog.logMessage(
                f"Task failed: {e}", LOG_TAG, level=Qgis.Critical
            )
            return False

    # ------------------------------------------------------------------

    def finished(self, result):
        if result:
            # Load the output CSV into QGIS now that we are back on the
            # main thread
            crs = "EPSG:3035"
            x_field = "easting"
            y_field = "northing"
            uri = (
                f"file:///{self.output}"
                f"?delimiter=,&xField={x_field}&yField={y_field}&crs={crs}"
            )
            layer = QgsVectorLayer(uri, self.layer_name, "delimitedtext")
            if layer.isValid():
                QgsProject.instance().addMapLayer(layer)
                QgsMessageLog.logMessage(
                    f"Layer '{self.layer_name}' loaded successfully.",
                    LOG_TAG, level=Qgis.Success
                )
            else:
                QgsMessageLog.logMessage(
                    f"Output CSV written but layer failed to load: {self.output}",
                    LOG_TAG, level=Qgis.Warning
                )

            if self.iface:
                self.iface.messageBar().pushMessage(
                    "EGMS Concatenation",
                    f"Done — layer '{self.layer_name}' loaded.",
                    level=Qgis.Success,
                    duration=6
                )
        else:
            if self.iface:
                self.iface.messageBar().pushMessage(
                    "EGMS Concatenation",
                    f"Processing failed: {self.error}",
                    level=Qgis.Critical,
                    duration=0
                )

    # ------------------------------------------------------------------

    def cancel(self):
        QgsMessageLog.logMessage("Task cancelled by user.", LOG_TAG, level=Qgis.Warning)
        super().cancel()


# ======================================================================
# HELPER WIDGET
# ======================================================================

class LayerOrFileWidget(QGroupBox):
    def __init__(self, title, file_filter="CSV (*.csv)", parent=None):
        super().__init__(title, parent)
        self.file_filter = file_filter

        outer = QVBoxLayout(self)
        outer.setSpacing(6)

        # ── Loaded-layers row ─────────────────────────────────────────
        lbl_layers = QLabel("Choose input layer:")
        bold = lbl_layers.font()
        bold.setBold(True)
        lbl_layers.setFont(bold)
        outer.addWidget(lbl_layers)

        self.combo = QComboBox()
        self.combo.setMinimumHeight(28)
        self.combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        outer.addWidget(self.combo)

        # ── Browse row ────────────────────────────────────────────────
        lbl_file = QLabel("Or browse for a file:")
        bold2 = lbl_file.font()
        bold2.setBold(True)
        lbl_file.setFont(bold2)
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

        # ── Signals ───────────────────────────────────────────────────
        browse_btn.clicked.connect(self._browse)
        self.combo.currentIndexChanged.connect(self._on_combo_changed)
        self.line.textEdited.connect(self._on_line_edited)

    # ------------------------------------------------------------------

    def populate(self, layer_types=None):
        """Fill the combo with layers currently loaded in QGIS."""
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItem("-- select a loaded layer --", userData=None)
        for layer in QgsProject.instance().mapLayers().values():
            if layer_types and layer.type() not in layer_types:
                continue
            self.combo.addItem(layer.name(), userData=layer.source())
        self.combo.blockSignals(False)

    def path(self):
        """Return the currently selected file path."""
        return self.line.text().strip()

    # ------------------------------------------------------------------

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select File", "", self.file_filter
        )
        if path:
            self.line.setText(path)
            self.combo.blockSignals(True)
            self.combo.setCurrentIndex(0)
            self.combo.blockSignals(False)

    def _on_combo_changed(self, index):
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


# ======================================================================
# MAIN DIALOG
# ======================================================================

class EGMSDialog(QDialog):

    def __init__(self, iface):
        super().__init__()

        self.iface = iface
        self._task  = None   
        self.setWindowTitle("EGMS Time Series Concatenation")
        self.setMinimumSize(700, 640)
        self.resize(740, 700)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(14)
        main_layout.setContentsMargins(16, 16, 16, 16)

        # ── Period 1 ──────────────────────────────────────────────────
        self.w_csv1 = LayerOrFileWidget(
            "EGMS input layer - Period 1 (csv)", file_filter="CSV files (*.csv)"
        )
        main_layout.addWidget(self.w_csv1)

        # ── Period 2 ──────────────────────────────────────────────────
        self.w_csv2 = LayerOrFileWidget(
            "EGMS input layer - Period 2 (csv)", file_filter="CSV files (*.csv)"
        )
        main_layout.addWidget(self.w_csv2)

        # ── AOI (optional) ────────────────────────────────────────────
        self.use_aoi = QCheckBox("Clip to AOI (optional)")
        f = self.use_aoi.font()
        f.setBold(True)
        self.use_aoi.setFont(f)
        main_layout.addWidget(self.use_aoi)

        self.w_aoi = LayerOrFileWidget(
            "AOI polygon shapefile", file_filter="Shapefile (*.shp)"
        )
        self.w_aoi.setEnabled(False)
        main_layout.addWidget(self.w_aoi)
        self.use_aoi.toggled.connect(self.w_aoi.setEnabled)

        # ── Output ────────────────────────────────────────────────────
        out_group = QGroupBox("Output")
        out_grid = QGridLayout(out_group)
        out_grid.setSpacing(8)

        out_grid.addWidget(QLabel("Output csv file:"), 0, 0)
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

        # ── Spacer ────────────────────────────────────────────────────
        main_layout.addSpacerItem(
            QSpacerItem(20, 10, QSizePolicy.Minimum, QSizePolicy.Expanding)
        )

        # ── Button bar ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        btn_row.addSpacerItem(
            QSpacerItem(40, 20, QSizePolicy.Expanding, QSizePolicy.Minimum)
        )

        self.run_button = QPushButton("Run")
        self.run_button.setMinimumHeight(36)
        self.run_button.setMinimumWidth(120)
        self.run_button.setDefault(True)
        rf = self.run_button.font()
        rf.setBold(True)
        self.run_button.setFont(rf)
        self.run_button.clicked.connect(self.execute)
        btn_row.addWidget(self.run_button)

        close_btn = QPushButton("Close")
        close_btn.setMinimumHeight(36)
        close_btn.setMinimumWidth(90)
        close_btn.clicked.connect(self.reject)
        btn_row.addWidget(close_btn)

        main_layout.addLayout(btn_row)

        # Populate layer combos
        self._refresh_layers()

    # ------------------------------------------------------------------

    def _refresh_layers(self):
        for w in (self.w_csv1, self.w_csv2, self.w_aoi):
            w.populate(layer_types=[QgsMapLayerType.VectorLayer])

    def _select_output(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Select Output CSV", "", "CSV files (*.csv)"
        )
        if path:
            if not path.lower().endswith(".csv"):
                path += ".csv"
            self.out_csv.setText(path)

    # ------------------------------------------------------------------

    def execute(self):
        import os

        csv1       = self.w_csv1.path()
        csv2       = self.w_csv2.path()
        output     = self.out_csv.text().strip()
        layer_name = os.path.splitext(os.path.basename(output))[0] if output else "EGMS_Fused_TS"

        if not csv1 or not csv2 or not output:
            QMessageBox.warning(
                self,
                "Missing inputs",
                "Please provide Period 1 CSV, Period 2 CSV, and an output file path."
            )
            return

        aoi_path = self.w_aoi.path() if self.use_aoi.isChecked() else None

        # Disable Run while the task is active so it cannot be submitted twice
        self.run_button.setEnabled(False)
        self.run_button.setText("Running…")

        # Create and queue the background task
        self._task = EGMSConcatTask(
            csv1, csv2, output, aoi_path, layer_name, self.iface
        )

        # Re-enable the Run button once the task finishes (success or failure)
        self._task.taskCompleted.connect(self._on_task_finished)
        self._task.taskTerminated.connect(self._on_task_finished)

        QgsApplication.taskManager().addTask(self._task)

        # Close the dialog immediately — processing continues in background
        self.accept()

    # ------------------------------------------------------------------

    def _on_task_finished(self):
        self.run_button.setEnabled(True)
        self.run_button.setText("Run")